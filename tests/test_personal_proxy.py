"""Optional HAProxy ingress acceptance using real HTTP, never live vendors.

Run with a stock HAProxy 2.8+ executable on PATH (or AGENTPEERS_HAPROXY).
The inherited journey verifies OAuth, native callbacks and events through the proxy.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import threading
import unittest

import test_personal_http as journey


HAPROXY = os.environ.get("AGENTPEERS_HAPROXY") or shutil.which("haproxy")


@unittest.skipUnless(HAPROXY, "Optional deployment check requires HAProxy")
class PersonalProxy(journey.PersonalHTTP):
    def setUp(self):
        super().setUp()
        self.backend_port = self.server.server_port
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.proxy_port = reserve.getsockname()[1]
        root = Path(__file__).resolve().parent.parent
        config = (root / "deploy/personal/haproxy.cfg").read_text()
        config = config.replace("127.0.0.1:8765", "127.0.0.1:" + str(self.proxy_port))
        config = config.replace("127.0.0.1:8766", "127.0.0.1:" + str(self.backend_port))
        config = config.replace("agentpeers.example.com", "bridge.example")
        config = config.replace("timeout http-request 15s", "timeout http-request 350ms")
        config = config.replace("wait-for-body time 15s", "wait-for-body time 350ms")
        self.proxy_config = Path(self.temp.name) / "haproxy.cfg"
        self.proxy_config.write_text(config)
        subprocess.run([HAPROXY, "-c", "-f", str(self.proxy_config)], check=True, capture_output=True)
        self.proxy = subprocess.Popen([HAPROXY, "-db", "-f", str(self.proxy_config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_proxy)
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=0.2):
                    break
            except OSError:
                if self.proxy.poll() is not None or time.monotonic() >= deadline:
                    self.fail("Fixture proxy did not start")
                time.sleep(0.02)

    def stop_proxy(self):
        if self.proxy.poll() is None:
            self.proxy.terminate()
        self.proxy.communicate(timeout=5)

    def tearDown(self):
        self.stop_proxy()
        super().tearDown()

    def request(self, method, path, body=None, token=None, headers=None):
        import http.client
        import json
        h = {"Host": "bridge.example", **(headers or {})}
        if token:
            h["Authorization"] = "Bearer " + token
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        connection.request(method, path, body=body, headers=h)
        response = connection.getresponse()
        status, response_headers, raw = response.status, response.headers, response.read()
        connection.close()
        return status, response_headers, json.loads(raw) if raw and "application/json" in response_headers.get("Content-Type", "") else raw.decode()

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2) as client:
            client.sendall(request)
            return client.recv(8192)

    def test_proxy_refuses_wrong_host_large_body_and_unsupported_method(self):
        self.assertEqual(self.request("GET", "/health", headers={"Host": "private.example"})[0], 421)
        self.assertEqual(self.request("DELETE", "/health")[0], 405)
        response = self.raw(b"POST /v1/call HTTP/1.1\r\nHost: bridge.example\r\nContent-Length: 262145\r\n\r\n")
        self.assertIn(b" 413 ", response)

    def test_proxy_accepts_exact_body_limit_with_real_authenticated_tool(self):
        body = '{"tool":"list_peers"}'
        body += " " * (262144 - len(body))
        status, _, value = self.request("POST", "/v1/call", body, "g" * 40,
                                       {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(value["peers"][0]["id"], "dot")

    def test_proxy_caps_simultaneous_backend_requests(self):
        original = self.server.RequestHandlerClass
        entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
        activity = {"active": 0, "maximum": 0}
        class Held(original):
            def do_GET(self):
                if self.path != "/held":
                    return super().do_GET()
                with lock:
                    activity["active"] += 1
                    activity["maximum"] = max(activity["maximum"], activity["active"])
                    if activity["active"] == 8:
                        entered.set()
                try:
                    release.wait(3)
                    self.output(200, {"held": True})
                finally:
                    with lock:
                        activity["active"] -= 1
        self.server.RequestHandlerClass = Held
        with ThreadPoolExecutor(max_workers=12) as clients:
            pending = [clients.submit(self.request, "GET", "/held") for _ in range(12)]
            try:
                self.assertTrue(entered.wait(2), "Eight backend slots should be admitted")
                time.sleep(0.1)
                self.assertEqual(activity["maximum"], 8)
            finally:
                release.set()
            statuses = [f.result(timeout=5)[0] for f in pending]
            self.assertEqual(statuses, [200] * 12, 'All admitted or queued requests should finish after release')

    def test_proxy_refuses_chunked_body_before_backend(self):
        response = self.raw(b"POST /v1/call HTTP/1.1\r\nHost: bridge.example\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n\r\n")
        self.assertIn(b" 400 ", response)

    def drip(self, start):
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2) as client:
            client.sendall(start)
            started = time.monotonic()
            # Every byte arrives well inside the 15-second idle timeout.
            for _ in range(8):
                time.sleep(0.08)
                try:
                    client.sendall(b"x")
                except OSError:
                    break
            try:
                response = client.recv(8192)
            except ConnectionResetError:
                response = b""
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(not response or b" 408 " in response)

    def test_proxy_total_header_deadline_survives_slow_drip(self):
        self.drip(b"GET /health HTTP/1.1\r\nHost: bridge.example\r\nX-Test: ")

    def test_proxy_total_body_deadline_survives_slow_drip(self):
        self.drip(b"POST /v1/call HTTP/1.1\r\nHost: bridge.example\r\nContent-Length: 100\r\n\r\n")


if __name__ == "__main__":
    unittest.main()

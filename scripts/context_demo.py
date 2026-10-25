#!/usr/bin/env python3
"""Run the isolated ChatGPT context contract with synthetic local receivers."""
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentpeers.personal.context import ContextProtocol, ContextRequests, Grant, Receiver, Requester


def main():
    # These are fixture identities, not credentials or authenticated live users.
    requester = Requester("fixture-owner", "fixture-user", "fixture-chat")
    grants = tuple(Grant(requester.owner, requester.subject, name, peer, "fixture-status", "Answer synthetic status facts only")
                   for name, peer in (("Dot", "fixture-dot"), ("Marketing", "codex:fixture-exact-session")))
    receipts = []
    with tempfile.TemporaryDirectory(prefix="agentpeers-context-demo-") as directory:
        with closing(ContextRequests(Path(directory) / "context.sqlite3", grants, clock=lambda: 1000.0)) as requests:
            protocol = ContextProtocol(requests)
            for index, grant in enumerate(grants):
                request_id = "fixture-request-" + str(index + 1)
                arguments = {"request_id": request_id, "destination": grant.destination, "topic": grant.topic, "question": "What status is in the fixture record?"}
                pending = protocol.call(requester, "request_context", arguments)
                duplicate = protocol.call(requester, "request_context", arguments)
                receiver = Receiver(grant.owner, grant.receiver)
                delivery_id = requests.pending(receiver)[0]
                envelope = requests.claim(receiver, delivery_id)
                requests.answer(receiver, delivery_id, "The synthetic project status is ready for review.",
                                [{"reference": "fixture:project-record@revision-7", "observed_at": 990}])
                result = protocol.call(requester, "context_result", {"request_id": request_id})
                receipts.append({"destination": grant.destination, "initial_state": pending["state"],
                                 "duplicate_suppressed": duplicate["duplicate"], "receiver_claimed": envelope["claimed"],
                                 "retrieved_result": result})
    print(json.dumps({"mode": "local synthetic fixtures only", "live_calls": 0, "receipts": receipts}, indent=2))


if __name__ == "__main__":
    main()

"""Bad addresses must not look like delivery failures or contact a peer."""
import os
import tempfile
import unittest
from unittest.mock import patch

from agentpeers.addressing import validate_destination
from agentpeers.core import Bridge
from agentpeers.mcp import InvalidArguments, dispatch


ACTOR = "claude:11111111-1111-4111-8111-111111111111"


class DestinationValidation(unittest.TestCase):
    def test_short_id_rejected_before_mcp_identity_discovery_or_mailbox(self):
        with patch("agentpeers.mcp.resolve_actor") as actor, patch("agentpeers.mcp.Bridge") as bridge:
            with self.assertRaises(InvalidArguments) as caught:
                dispatch("send_message", {"to": "codex:01234567", "message": "heads-up",
                                          "scope": "authorized coordination"})
        result = caught.exception.result()
        self.assertEqual(result["fields"], ["to"])
        self.assertFalse(result["message_created"])
        self.assertFalse(result["submission_attempted"])
        self.assertIn("shortened IDs", result["error"])
        self.assertIn("remote Codex", result["error"])
        actor.assert_not_called()
        bridge.assert_not_called()

    def test_direct_core_rejects_invalid_id_without_saved_message_or_discovery(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"AGENTPEERS_HOME": folder}), \
                patch("agentpeers.core.inventory") as inventory:
            with Bridge() as bridge:
                for destination in ("codex:01234567", "claude:", "codex:not-a-uuid"):
                    with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "address error"):
                        bridge.send(ACTOR, destination, "heads-up", "authorized coordination")
                self.assertEqual(bridge.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        inventory.assert_not_called()

    def test_setup_also_rejects_short_id_before_probe(self):
        with patch("agentpeers.mcp.start_probe") as probe:
            with self.assertRaises(InvalidArguments):
                dispatch("setup", {"to": "claude:01234567", "scope": "authorized test"}, actor=ACTOR)
        probe.assert_not_called()

    def test_full_ids_and_plain_names_remain_valid_without_guessing(self):
        for address in (ACTOR, "codex:22222222-2222-4222-8222-222222222222", "reviewer", "Dotty coordinator"):
            with self.subTest(address=address):
                validate_destination(address)


if __name__ == "__main__":
    unittest.main()

"""Plow Chat plugin (card f864c568e6): the owner's iMessages reach the Boss exactly once.

The bridge polls, so what must hold: turning it on never replays history at the Boss, each
new message is delivered once, and a failed delivery is retried instead of skipped.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

PLUGINS = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "plugins"


def load(name, rel, env):
    with mock.patch.dict(os.environ, env):
        loader = importlib.machinery.SourceFileLoader(name, str(PLUGINS / rel))
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


class PlowChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pc = load("plow_chat", "plow-chat/plow-chat.py",
                       {"PLOW_CHAT_STATE_DIR": self.tmp.name, "HOST_ID": "h"})
        self.msgs = [{"uid": "m1", "direction": "inbound", "body": "old", "chat_uid": "cht_a",
                      "created_at": "1"}]
        self.pc.api = lambda method, path, body=None, token=None: (200, {"data": self.msgs})
        self.sent = []
        self.pc.send_to_boss = lambda m: self.sent.append(m) or True
        self.creds = {"token": "t", "chat_uid": "cht_a"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_seeds_then_routes_once(self):
        self.assertEqual(self.pc.BOSS_AGENT, "h/main:Boss")
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(self.sent, [], "history must not replay at the Boss")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi boss",
                          "chat_uid": "cht_a", "created_at": "2",
                          "sender": {"display_name": "Dan"}})
        self.msgs.append({"uid": "m3", "direction": "outbound", "body": "reply",
                          "chat_uid": "cht_a", "created_at": "3"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("[plowchat] from Dan in cht_a: hi boss", self.sent[0])
        self.assertIn("reply cht_a", self.sent[0])

    def test_failed_delivery_is_retried(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi",
                          "chat_uid": "cht_a", "created_at": "2"})
        self.pc.send_to_boss = lambda m: False
        with mock.patch.object(self.pc, "send_message") as warn:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        warn.assert_called_once()  # the sender hears it did not land
        self.pc.send_to_boss = lambda m: self.sent.append(m) or True
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)



class WiringTest(unittest.TestCase):
    def test_supervise_starts_it_only_when_turned_on_and_down_stops_it(self):
        root = PLUGINS.parents[2]
        sup = (root / "mypeople" / "runtime" / "bin" / "supervise.sh").read_text()
        self.assertIn('[ -n "${PLOW_CHAT:-}" ] && ensure "$ID/plugins/plow-chat/plow-chat.py"', sup)
        self.assertIn('"plugins", "plow-chat", "plow-chat.py"', (root / "mypeople" / "cli.py").read_text())


if __name__ == "__main__":
    unittest.main()

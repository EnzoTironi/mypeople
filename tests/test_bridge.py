"""Exercise the Plow -> local mp -> correlated reply boundary without credentials."""
from concurrent.futures import ThreadPoolExecutor
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from mypeople.bridge import Bridge, BridgeError


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = []

        class Plow(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer test-token":
                    self.send_error(401)
                    return
                if self.path != "/v1/chats/cht_shared/messages":
                    self.send_error(404)
                    return
                body = json.dumps({"messages": cls.messages}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Plow)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {"INSTALL_DIR": str(self.root), "HOST_ID": "sam"}
        self.bridge = Bridge(self.cfg, self.root / "config" / "bridge.json")
        (self.root / "run").mkdir()
        (self.root / "run" / "roster.json").write_text(json.dumps({
            "sam/main:coder": {"backend": "codex", "cwd": "/private-project"},
            "sam/main:private": {"backend": "claude", "summary": "secret"}}))
        (self.root / "token").write_text("test-token\n")
        with patch.dict(os.environ, {}, clear=True):
            self.bridge.configure(["coder=sam/main:coder"], ["cht_shared"], self.root / "token",
                                  "http://127.0.0.1:" + str(self.server.server_port))
        self.messages[:] = [{"uid": "msg_original", "chat_uid": "cht_shared", "direction": "inbound",
                             "sender": {"type": "member", "display_name": "Guest"},
                             "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             "body": "Explain this project's entry point. `touch /tmp/never` $(echo no)"}]
        self.send = patch("mypeople.bridge.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "sent", ""))
        self.dispatch = self.send.start()
        self.addCleanup(self.send.stop)

    def ask(self):
        return self.bridge.ask("cht_shared", "msg_original", "coder")

    def reply_key(self):
        command = next(line for line in self.dispatch.call_args.kwargs["input"].splitlines() if line.startswith("env "))
        args = shlex.split(command)
        return args[args.index("--key") + 1]

    def test_roundtrip_survives_restart_and_replies_only_to_source_chat(self):
        receipt = self.ask()
        self.assertEqual(receipt["status"], "submitted")
        with patch.dict(os.environ, {"AGENT_ID": "sam/main:coder"}):
            self.bridge.reply(receipt["request"], "The entry point is cli.main.", self.reply_key())
        restarted = Bridge(self.cfg, self.bridge.config_path)
        self.assertEqual(restarted.result("cht_shared", receipt["request"])["reply"], "The entry point is cli.main.")
        with self.assertRaisesRegex(BridgeError, "chat_not_shared"):
            restarted.result("cht_other", receipt["request"])
        self.assertEqual((self.bridge.state / "requests.sqlite3").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.bridge.config_path.stat().st_mode & 0o777, 0o600)

    def test_guest_cannot_select_private_agent_or_unshared_chat(self):
        with self.assertRaisesRegex(BridgeError, "chat_not_shared"):
            self.bridge.ask("cht_other", "msg_original", "coder")
        with self.assertRaisesRegex(BridgeError, "agent_not_shared"):
            self.bridge.ask("cht_shared", "msg_original", "private")
        self.dispatch.assert_not_called()
        self.assertEqual(self.bridge.agents("cht_shared")["agents"], [{"alias": "coder", "backend": "codex", "status": "unknown"}])

    def test_prompt_is_verified_at_plow_and_never_a_shell_argument(self):
        receipt = self.ask()
        call = self.dispatch.call_args
        self.assertEqual(call.args[0], [str(self.root / "bin" / "mp"), "send", "sam/main:coder"])
        self.assertIn(self.messages[0]["body"], call.kwargs["input"])
        self.assertNotIn("shell", call.kwargs)
        self.assertIn(receipt["request"], call.kwargs["input"])
        self.assertNotIn("test-token", call.kwargs["input"])
        self.assertNotIn("AGENT_ID", call.kwargs["env"])
        self.assertIn("MYPEOPLE_CONFIG_PATH=", call.kwargs["input"])
        self.assertNotIn(self.reply_key(), json.dumps(receipt))
        with self.bridge.ledger() as db:
            self.assertNotEqual(db.execute("SELECT reply_key_hash FROM requests").fetchone()[0], self.reply_key())

    def test_concurrent_retries_send_once(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            receipts = list(executor.map(lambda _: self.ask(), range(4)))
        self.assertEqual(len({r["request"] for r in receipts}), 1)
        self.assertEqual(self.dispatch.call_count, 1)

    def test_outbound_peer_agent_stale_and_foreign_messages_are_refused(self):
        original = dict(self.messages[0])
        variants = [{"direction": "outbound"}, {"sender": {"type": "agent"}},
                    {"chat_uid": "cht_foreign"}, {"created_at": "2020-01-01T00:00:00Z"},
                    {"created_at": "invalid"}, {"body": ""}, {"sender": []}]
        for change in variants:
            with self.subTest(change=change):
                self.messages[:] = [dict(original, **change)]
                with self.assertRaises(BridgeError):
                    self.ask()
        self.messages[:] = []
        with self.assertRaises(BridgeError):
            self.ask()
        self.dispatch.assert_not_called()

    def test_unready_agent_and_uncertain_delivery_do_not_replay(self):
        self.dispatch.return_value = subprocess.CompletedProcess([], 3, "not_ready", "")
        receipt = self.ask()
        self.assertEqual(receipt["status"], "not_ready")
        self.assertEqual(self.ask(), receipt)
        self.assertEqual(self.dispatch.call_count, 1)
        self.messages[0]["uid"] = "msg_timeout"
        self.dispatch.side_effect = subprocess.TimeoutExpired("mp", 45)
        receipt = self.bridge.ask("cht_shared", "msg_timeout", "coder")
        self.assertEqual(receipt["status"], "delivery_unknown")
        self.assertEqual(self.bridge.ask("cht_shared", "msg_timeout", "coder"), receipt)
        self.assertEqual(self.dispatch.call_count, 2)

    def test_only_request_key_can_reply_and_reply_cannot_change(self):
        receipt = self.ask()
        with patch.dict(os.environ, {"AGENT_ID": "sam/main:coder"}):
            with self.assertRaisesRegex(BridgeError, "invalid_reply_key"):
                self.bridge.reply(receipt["request"], "no", "wrong-key")
        with patch.dict(os.environ, {}, clear=True):
            self.bridge.reply(receipt["request"], "yes", self.reply_key())
            self.bridge.reply(receipt["request"], "yes", self.reply_key())
            with self.assertRaisesRegex(BridgeError, "already_replied"):
                self.bridge.reply(receipt["request"], "different", self.reply_key())

    def test_revoking_chat_or_agent_revokes_existing_requests(self):
        receipt = self.ask()
        config = self.bridge.config()
        config["chats"] = []
        self.bridge.config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(BridgeError, "chat_not_shared"):
            self.bridge.result("cht_shared", receipt["request"])
        config["chats"] = ["cht_shared"]
        config["agents"] = {}
        self.bridge.config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(BridgeError, "request_not_shared"):
            self.bridge.result("cht_shared", receipt["request"])
        with patch.dict(os.environ, {"AGENT_ID": "sam/main:coder"}):
            with self.assertRaisesRegex(BridgeError, "agent_not_shared"):
                self.bridge.reply(receipt["request"], "no", self.reply_key())

    def test_reply_key_is_bound_to_its_request_and_expires(self):
        first = self.ask()
        first_key = self.reply_key()
        self.messages[0]["uid"] = "msg_second"
        second = self.bridge.ask("cht_shared", "msg_second", "coder")
        second_key = self.reply_key()
        with self.assertRaisesRegex(BridgeError, "invalid_reply_key"):
            self.bridge.reply(second["request"], "wrong request", first_key)
        self.bridge.reply(first["request"], "first reply", first_key)
        self.bridge.clock = lambda: __import__("time").time() + 901
        with self.assertRaisesRegex(BridgeError, "not_awaiting_reply"):
            self.bridge.reply(second["request"], "late reply", second_key)

    def test_request_times_out_without_inventing_an_answer(self):
        receipt = self.ask()
        self.bridge.clock = lambda: __import__("time").time() + 901
        result = self.bridge.result("cht_shared", receipt["request"])
        self.assertEqual(result["status"], "timed_out")
        self.assertNotIn("reply", result)

    def test_configuration_refuses_remote_agents_and_agent_initiated_grants(self):
        with self.assertRaisesRegex(BridgeError, "only_local"):
            self.bridge.configure(["coder=other/main:coder"], ["cht_shared"], self.root / "token", "https://api.plow.co")
        with patch.dict(os.environ, {"AGENT_ID": "sam/main:coder"}):
            with self.assertRaisesRegex(BridgeError, "owner_terminal"):
                self.bridge.configure(["coder=sam/main:coder"], ["cht_shared"], self.root / "token", "https://api.plow.co")


if __name__ == "__main__":
    unittest.main()

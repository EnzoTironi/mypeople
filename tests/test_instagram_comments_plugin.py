"""The Instagram comment watcher delivers each new comment once, never history and never our own.

Instagram and `mp send` are stubbed, so nothing leaves the box.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "mypeople" / "runtime" / "plugins" / "instagram-comments" / "instagram-comments.py"
POST = "https://www.instagram.com/reel/abc/"


def load(state_dir):
    env = {"INSTAGRAM_COMMENTS_STATE_DIR": state_dir, "HOST_ID": "node",
           "INSTAGRAM_TOKEN": "sekrit", "INSTAGRAM_USER_ID": "1784"}
    with mock.patch.dict(os.environ, env):
        loader = importlib.machinery.SourceFileLoader("ig_%d" % id(state_dir), str(PLUGIN))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod


def c(cid, user="fan", text="nice"):
    return {"id": cid, "username": user, "text": text}


class InstagramCommentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_first_sighting_silent_then_only_new_and_never_our_own(self):
        state = {}
        self.assertEqual([], self.m.fresh_comments("m1", [c("1")], state, "danedelattre"))
        got = self.m.fresh_comments("m1", [c("1"), c("2"), c("3", user="DaneDelattre")], state,
                                    "danedelattre")
        self.assertEqual(["2"], [x["id"] for x in got])
        self.assertEqual([], self.m.fresh_comments("m1", [c("1"), c("2")], state, "danedelattre"))

    def test_poll_sends_the_reply_to_ig_shape_to_the_boss(self):
        sent = []
        with mock.patch.dict(os.environ, {"INSTAGRAM_TOKEN": "sekrit", "INSTAGRAM_USER_ID": "1784",
                                          "HOST_ID": "node"}), \
             mock.patch.object(self.m, "recent_media", return_value=[("m1", POST)]), \
             mock.patch.object(self.m, "deliver", side_effect=lambda a, t: sent.append((a, t)) or True):
            state = {}
            with mock.patch.object(self.m, "media_comments", return_value=[c("1")]):
                self.m.poll(state, "danedelattre")
            with mock.patch.object(self.m, "media_comments",
                                   return_value=[c("1"), c("2", text="how  do I\njoin?")]):
                self.m.poll(state, "danedelattre")
        self.assertEqual([("node/main:Boss",
                           "[IG REPLY] comment_id=2 user=fan permalink=%s: how do I join?" % POST)], sent)

    def test_graph_errors_never_leak_the_token(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":"token sekrit bad"}'))
        with mock.patch.dict(os.environ, {"INSTAGRAM_TOKEN": "sekrit"}), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                self.m.graph("1784/media")
        self.assertNotIn("sekrit", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

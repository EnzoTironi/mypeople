#!/usr/bin/env python3
"""Instagram comment watcher: new comments on the account's posts reach the agent that answers them.

Every poll this reads the comments (and thread replies) on the account's most recent posts through
the Instagram Graph API and delivers each NEW one with `mp send`, in the shape the reply-to-ig
skill expects:

    [IG REPLY] comment_id=<ID> user=<username> permalink=<POST URL>: <text>

It only reads. Answering is the receiving agent's job (reply-to-ig), so nothing public happens
unless that agent decides to reply.

Turn it on in ~/.config/mypeople/queue.env, then restart the daemons:

    export INSTAGRAM_COMMENTS=1
    export INSTAGRAM_TOKEN="..."                # token with instagram_basic + instagram_manage_comments
    export INSTAGRAM_USER_ID="1784..."          # the Instagram business account id
    export INSTAGRAM_COMMENTS_AGENT="host/main:ig"  # optional: default is the Boss
    export INSTAGRAM_COMMENTS_MEDIA=10          # optional: how many recent posts to watch
    export INSTAGRAM_COMMENTS_INTERVAL=60       # optional: seconds between polls

The first time a post is seen its existing comments are recorded without being sent, so turning
this on never replays a post's history at anyone. The account's own comments are never delivered.

    instagram-comments.py serve    poll forever (what supervise.sh runs)
    instagram-comments.py once     one poll, then exit
    instagram-comments.py status   account, watched posts, and where comments go
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / ".local/share/mypeople")
STATE = Path(os.environ.get("INSTAGRAM_COMMENTS_STATE_DIR")
             or INSTALL / "state" / "instagram-comments") / "state.json"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
GRAPH = "https://graph.facebook.com/v21.0/"
MAX_PAGES = 10   # per post per poll; a post drawing >500 comments between polls loses the rest
SNIPPET = 400


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + "[instagram-comments] " + msg, flush=True)


def cfg(key, default=""):
    """Env first, then queue.env -- the same precedence as the rest of the runtime."""
    if os.environ.get(key):
        return os.environ[key]
    path = os.environ.get("MYPEOPLE_CONFIG_PATH") or str(Path.home() / ".config/mypeople/queue.env")
    try:
        for line in open(path):
            line = line.strip()
            line = line[7:] if line.startswith("export ") else line
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return default


# ---------------------------------------------------------------- Instagram
def graph(path_or_url, **params):
    token = cfg("INSTAGRAM_TOKEN")
    if path_or_url.startswith("https://"):
        url = path_or_url   # a paging "next" link already carries the token
    else:
        url = GRAPH + path_or_url + "?" + urllib.parse.urlencode(dict(params, access_token=token))
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        # Meta echoes the request back in some errors; the token must never reach a log.
        raise RuntimeError("graph %s: HTTP %d %s" % (path_or_url.split("?")[0][:60], e.code,
                                                     body.replace(token, "<token>") if token else body))


def own_username():
    return graph(cfg("INSTAGRAM_USER_ID"), fields="username").get("username", "")


def recent_media():
    n = int(cfg("INSTAGRAM_COMMENTS_MEDIA", "10"))
    data = graph(cfg("INSTAGRAM_USER_ID") + "/media", fields="id,permalink", limit=n)
    return [(m["id"], m.get("permalink", "")) for m in data.get("data", [])[:n]]


def media_comments(media_id):
    """Every comment on a post, thread replies included, as flat dicts."""
    out, page = [], graph(media_id + "/comments", limit=50,
                          fields="id,text,username,timestamp,replies{id,text,username,timestamp}")
    for _ in range(MAX_PAGES):
        for c in page.get("data", []):
            out.append(c)
            out.extend((c.get("replies") or {}).get("data", []))
        nxt = (page.get("paging") or {}).get("next")
        if not nxt:
            break
        page = graph(nxt)
    return out


# ---------------------------------------------------------------- what is new
def fresh_comments(media_id, comments, state, me):
    """New comments on this post, recording everything as seen. A post's first sighting is silent."""
    seen = set(state.setdefault("seen", []))
    known = set(state.setdefault("media", []))
    new = [c for c in comments if c["id"] not in seen]
    state["seen"] = sorted(seen | {c["id"] for c in comments})
    if media_id not in known:
        state["media"] = sorted(known | {media_id})
        return []
    return [c for c in new if (c.get("username") or "").lower() != me.lower()]


def format_comment(c, permalink):
    text = " ".join((c.get("text") or "").split())
    if len(text) > SNIPPET:
        text = text[:SNIPPET - 1] + "…"
    return "[IG REPLY] comment_id=%s user=%s permalink=%s: %s" % (
        c["id"], c.get("username") or "?", permalink, text or "(no text)")


# ---------------------------------------------------------------- the fleet
def target():
    return cfg("INSTAGRAM_COMMENTS_AGENT") or "%s/main:Boss" % cfg(
        "HOST_ID", os.uname().nodename.split(".")[0])


def deliver(agent, text):
    r = subprocess.run([sys.executable, MP_BIN, "send", agent, text], capture_output=True, text=True,
                       timeout=60)
    return r.returncode == 0


# ---------------------------------------------------------------- loop
def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)


def poll(state, me):
    outbox = []
    for media_id, permalink in recent_media():
        for c in fresh_comments(media_id, media_comments(media_id), state, me):
            outbox.append(format_comment(c, permalink))
    # Seen-state is saved before sending: a crash mid-send drops a delivery rather than
    # re-sending comments the agent may already have answered in public.
    save_state(state)
    agent = target()
    for text in outbox:
        if not deliver(agent, text):
            log("mp send to %s failed: %s" % (agent, text[:120]))
    if outbox:
        log("delivered %d new comment(s) to %s" % (len(outbox), agent))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    if not (cfg("INSTAGRAM_TOKEN") and cfg("INSTAGRAM_USER_ID")):
        log("INSTAGRAM_TOKEN and INSTAGRAM_USER_ID must be set in queue.env")
        return 2
    me = own_username()
    if cmd == "status":
        media = recent_media()
        print("@%s: watching %d recent post(s), new comments go to %s" % (me, len(media), target()))
        for _, permalink in media:
            print("  " + permalink)
        return 0
    state = load_state()
    if cmd == "once":
        poll(state, me)
        return 0
    interval = int(cfg("INSTAGRAM_COMMENTS_INTERVAL", "60"))
    log("watching @%s every %ds" % (me, interval))
    while True:
        try:
            poll(state, me)
        except Exception as e:  # a Graph API hiccup must not kill the watcher
            log("poll failed: %s" % e)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Instagram comment receiver: Meta pushes each new comment here, and it reaches the agent that answers.

Push, not polling. The Facebook app's `instagram` webhook (field `comments`) POSTs every new
comment on the account's posts to this server. Each one is checked against the app secret,
written to a spool on disk, and forwarded through the fleet queue -- the same path a cross-host
`mp send` takes -- in the shape the reply-to-ig skill reads:

    [IG REPLY] comment_id=<ID> user=<username> permalink=<POST URL>: <text>

Run it on a host that does not sleep. When the fleet is unreachable (its Mac asleep) comments
stay in the spool and are retried until they land, so nothing is dropped. It never posts to
Instagram; answering is the receiving agent's job.

Config (env, or the file MYPEOPLE_CONFIG_PATH names, default ~/.config/mypeople/queue.env):

    INSTAGRAM_COMMENTS=1
    INSTAGRAM_APP_SECRET=...          # verifies Meta's X-Hub-Signature-256
    INSTAGRAM_VERIFY_TOKEN=...        # answers Meta's subscribe handshake
    INSTAGRAM_TOKEN=...               # read-only use: looks up a post's permalink
    INSTAGRAM_USER_ID=1784...         # the account; its own comments are never forwarded
    INSTAGRAM_COMMENTS_AGENT=host/main:Boss   # optional: default is this host's Boss
    INSTAGRAM_WEBHOOK_PORT=8796       # local port; expose it with `tailscale funnel`
    QUEUE_URL / QUEUE_SECRET          # the fleet queue to deliver through

    instagram-comments.py serve    receive + forward forever (what supervise.sh / systemd runs)
    instagram-comments.py status   pending spool and where comments go
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / ".local/share/mypeople")
STATE_DIR = Path(os.environ.get("INSTAGRAM_COMMENTS_STATE_DIR") or INSTALL / "state" / "instagram-comments")
SPOOL = STATE_DIR / "spool.jsonl"
SEEN = STATE_DIR / "seen.json"
GRAPH = "https://graph.facebook.com/v21.0/"
RETRY_SECS = 30   # only while something is undelivered; an empty spool waits for the next push
SNIPPET = 400
LOCK = threading.Lock()
WAKE = threading.Event()


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + "[instagram-comments] " + msg, flush=True)


def cfg(key, default=""):
    """Env first, then the config file -- the same precedence as the rest of the runtime."""
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


# ---------------------------------------------------------------- what Meta sends
def signature_ok(body, header):
    secret = cfg("INSTAGRAM_APP_SECRET").encode()
    want = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(want, header or "")


def comments_in(payload, own_id):
    """The new comments in one webhook body, minus the account's own."""
    out = []
    if payload.get("object") != "instagram":
        return out
    for entry in payload.get("entry") or []:
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            if ch.get("field") != "comments" or not v.get("id"):
                continue
            who = v.get("from") or {}
            if own_id and str(who.get("id")) == str(own_id):
                continue
            out.append({"id": str(v["id"]), "text": v.get("text") or "", "user": who.get("username") or "?",
                        "media": str((v.get("media") or {}).get("id") or "")})
    return out


def format_comment(c, permalink):
    text = " ".join(c["text"].split())
    if len(text) > SNIPPET:
        text = text[:SNIPPET - 1] + "…"
    return "[IG REPLY] comment_id=%s user=%s permalink=%s: %s" % (
        c["id"], c["user"], permalink or "media:" + c["media"], text or "(no text)")


# ---------------------------------------------------------------- spool
def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def spool_add(comments):
    """Append comments not seen before. Meta retries a delivery it thinks failed; a comment is
    spooled once."""
    with LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        seen = set(read_json(SEEN, []))
        fresh = [c for c in comments if c["id"] not in seen]
        if fresh:
            with open(SPOOL, "a") as f:
                for c in fresh:
                    f.write(json.dumps(c) + "\n")
                f.flush()
                os.fsync(f.fileno())
            tmp = SEEN.with_suffix(".tmp")
            # ponytail: seen ids grow forever (~25 bytes each); trim to the last N if it ever matters
            tmp.write_text(json.dumps(sorted(seen | {c["id"] for c in fresh})))
            tmp.replace(SEEN)
    return fresh


def spool_pending():
    with LOCK:
        try:
            return [json.loads(l) for l in SPOOL.read_text().splitlines() if l.strip()]
        except OSError:
            return []


def spool_drop(ids):
    with LOCK:
        keep = [l for l in (SPOOL.read_text().splitlines() if SPOOL.exists() else [])
                if l.strip() and json.loads(l)["id"] not in ids]
        tmp = SPOOL.with_suffix(".tmp")
        tmp.write_text("".join(l + "\n" for l in keep))
        tmp.replace(SPOOL)


# ---------------------------------------------------------------- delivery
def http_json(method, url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def permalink(media_id, cache={}):
    if media_id and media_id not in cache:
        try:
            q = urllib.parse.urlencode({"fields": "permalink", "access_token": cfg("INSTAGRAM_TOKEN")})
            cache[media_id] = http_json("GET", GRAPH + media_id + "?" + q).get("permalink", "")
        except Exception:
            return ""   # not cached: the next comment on this post tries again
    return cache.get(media_id, "")


def target():
    return cfg("INSTAGRAM_COMMENTS_AGENT") or "%s/main:Boss" % cfg(
        "HOST_ID", os.uname().nodename.split(".")[0])


def deliver(agent, text):
    """Cross-host `mp send`: submit a send task to the fleet queue and wait for its result."""
    base, hdr = cfg("QUEUE_URL", "http://127.0.0.1:9900"), {"X-Queue-Secret": cfg("QUEUE_SECRET")}
    tid = http_json("POST", base + "/task/submit",
                    {"type": "send", "target_agent": agent, "payload": {"message": text}}, hdr)["task_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        st = http_json("GET", base + "/task/" + tid, None, hdr)
        if st.get("ok") is not None:
            return bool(st["ok"])
        time.sleep(0.5)
    return False


def forward_loop():
    agent = target()
    while True:
        pending = spool_pending()
        done = set()
        for c in pending:
            try:
                if deliver(agent, format_comment(c, permalink(c["media"]))):
                    done.add(c["id"])
                else:
                    break
            except Exception as e:   # fleet asleep or unreachable: keep it spooled, retry later
                log("delivery to %s waiting: %s" % (agent, str(e)[:120]))
                break
        if done:
            spool_drop(done)
            log("delivered %d comment(s) to %s" % (len(done), agent))
        WAKE.wait(RETRY_SECS if len(pending) > len(done) else None)
        WAKE.clear()


# ---------------------------------------------------------------- HTTP
class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ok = (q.get("hub.mode") == ["subscribe"] and cfg("INSTAGRAM_VERIFY_TOKEN")
              and hmac.compare_digest(q.get("hub.verify_token", [""])[0], cfg("INSTAGRAM_VERIFY_TOKEN")))
        self.reply(200, q["hub.challenge"][0].encode()) if ok and q.get("hub.challenge") else self.reply(403)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self.reply(413)
        body = self.rfile.read(n)
        if not signature_ok(body, self.headers.get("X-Hub-Signature-256")):
            log("rejected a POST with a bad signature")
            return self.reply(403)
        try:
            payload = json.loads(body)
            fresh = spool_add(comments_in(payload, cfg("INSTAGRAM_USER_ID")))
        except ValueError:
            return self.reply(400)
        # One line per push proves Meta is reaching us, even for events we do not forward.
        fields = sorted({ch.get("field") or "messaging" for e in payload.get("entry") or []
                         for ch in (e.get("changes") or e.get("messaging") or [{}])})
        log("push %s %s: %d new comment(s)" % (payload.get("object"), ",".join(fields), len(fresh)))
        if fresh:
            WAKE.set()
        self.reply(200, b"ok")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    missing = [k for k in ("INSTAGRAM_APP_SECRET", "INSTAGRAM_VERIFY_TOKEN", "INSTAGRAM_USER_ID") if not cfg(k)]
    if missing:
        log("missing config: " + ", ".join(missing))
        return 2
    if cmd == "status":
        print("%d comment(s) waiting in %s; they go to %s via %s"
              % (len(spool_pending()), SPOOL, target(), cfg("QUEUE_URL", "http://127.0.0.1:9900")))
        return 0
    port = int(cfg("INSTAGRAM_WEBHOOK_PORT", "8796"))
    threading.Thread(target=forward_loop, daemon=True).start()
    log("listening on 127.0.0.1:%d, delivering to %s" % (port, target()))
    ThreadingHTTPServer(("127.0.0.1", port), Hook).serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv))

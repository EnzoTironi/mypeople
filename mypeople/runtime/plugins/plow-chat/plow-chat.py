#!/usr/bin/env python3
"""Plow Chat plugin: the owner texts the Boss over iMessage/SMS through Plow.

Turn it on with PLOW_CHAT=1 in queue.env; `mypeople up` then keeps it running.
First run has no credentials, so it mints a code: run `plow-chat.py status`
and text "Plow Activate: <code>" to the number it prints, once.

Usage:
  plow-chat.py serve                  run the bridge (supervise.sh entry point)
  plow-chat.py reply "text"           reply in the owner's own chat
  plow-chat.py reply cht_x "text"     reply in that chat (groups)
  plow-chat.py status                 print activation/bridge state

One account covers every chat: the owner can add the Plow line to group
threads, and each group is its own chat that reaches the Boss and is answered
in the thread that asked.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("PLOW_CHAT_BASE_URL", "https://api.plow.co")
INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / "mypeople")
STATE_DIR = Path(os.environ.get("PLOW_CHAT_STATE_DIR") or INSTALL / "state" / "plow-chat")
CREDS = STATE_DIR / "creds.json"          # {"token": ..., "chat_uid": ...}
ACTIVATION = STATE_DIR / "activation.json"
STATE = STATE_DIR / "state.json"          # {"seen_ids": [...], "seeded_chats": [...]}
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
# ponytail: polls every chat each POLL_SECONDS instead of holding Plow's
# websocket, so the plugin needs no dependency beyond the stdlib. Ceiling: a
# text lands up to POLL_SECONDS late and each pass costs 1 + chats requests;
# upgrade path is the account websocket (POST /v1/ws/ticket) via `websockets`.
POLL_SECONDS = float(os.environ.get("PLOW_CHAT_POLL_SECONDS", "5"))
SELF = str(Path(__file__).resolve())
STATE_LOCK = threading.Lock()


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] plow-chat: {msg}",
          flush=True)


def api(method: str, path: str, body=None, token=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def write_json(p: Path, obj):
    # Creds hold a user-wide bearer token: 600 from the first byte, never chmod after.
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


def listing(data) -> list:
    return data.get("data") or data.get("messages") or data.get("chats") or [] \
        if isinstance(data, dict) else data


# --- Boss ---

def envelope(who: str, text: str, chat_uid: str) -> str:
    # Plow seats whoever the carrier reports in the thread, so the Boss is told
    # who actually spoke and which thread — in a group these differ per message.
    return (
        f"[plowchat] from {who} in {chat_uid}: {text}\n"
        f"(This is the owner's Plow messages line. The owner texts here, and so "
        f"does anyone they added to this thread. To answer, run: "
        f"python3 {SELF} reply {chat_uid} \"your reply\" — it goes back to that "
        f"same thread, which may be a group. Plain text only, no markdown.)"
    )


def send_to_boss(message: str) -> bool:
    try:
        r = subprocess.run([sys.executable, MP_BIN, "send", BOSS_AGENT, message],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"ERROR mp send: {e}")
        return False
    if r.returncode != 0:
        log(f"ERROR mp send rc={r.returncode}: {r.stderr.strip()[:200]}")
        return False
    return True


def route_message(message: dict) -> str:
    """Deliver one inbound message to the Boss, exactly once."""
    if message.get("direction") != "inbound":
        return "skip: outbound"
    mid = message.get("uid") or ""
    text = (message.get("body") or "").strip()
    if not mid or not text:
        return "skip: empty"

    # Claim before sending, so an overlapping pass cannot wake the Boss twice.
    with STATE_LOCK:
        st = read_json(STATE, {})
        seen = list(st.get("seen_ids", []))
        if mid in seen:
            return "skip: already routed"
        st["seen_ids"] = (seen + [mid])[-1000:]
        write_json(STATE, st)

    sender = message.get("sender") or {}
    who = sender.get("display_name") or sender.get("provider_key") or "chat member"
    chat_uid = message.get("chat_uid") or ""
    if send_to_boss(envelope(who, text, chat_uid)):
        return f"ROUTE {chat_uid} {who}: {text[:60]}"
    with STATE_LOCK:  # release the claim so the next pass retries it
        st = read_json(STATE, {})
        st["seen_ids"] = [i for i in st.get("seen_ids", []) if i != mid]
        write_json(STATE, st)
    # Say it on the phone that just texted, the one path known to work, instead
    # of leaving them waiting on an answer nobody heard.
    try:
        send_message("Your text reached me but I could not wake the team with it. "
                     "Retrying; nothing is lost.", chat_uid)
    except (SystemExit, OSError) as exc:
        log(f"could not warn the sender either: {exc}")
    return "ERROR mp send failed; sender warned, retrying next pass"


# --- Outbound ---

def send_message(text: str, chat_uid: str = "") -> dict:
    creds = read_json(CREDS, {})
    if not creds.get("token"):
        raise SystemExit("no Plow Chat credentials yet: activation is not done")
    # No thread named = the owner's own 1:1, the chat activation resolved.
    status, data = api("POST", f"/v1/chats/{chat_uid or creds['chat_uid']}/messages",
                       {"body": text}, token=creds["token"])
    if status >= 400:
        raise SystemExit(f"send failed {status}: {json.dumps(data)[:200]}")
    return data


# --- Activation ---

def mint_activation() -> dict:
    status, data = api("POST", "/v1/auth/activate",
                       {"name": "MyPeople Boss", "provision_chat": False})
    if status >= 400 or "activation_secret" not in data:
        raise RuntimeError(f"activate failed {status}: {json.dumps(data)[:200]}")
    write_json(ACTIVATION, data)
    log(f"activation code: text \"Plow Activate: {data['display_code']}\" to {data['send_to']}")
    return data


def last_message_ts(token: str, chat_uid: str) -> str:
    _, data = api("GET", f"/v1/chats/{chat_uid}/messages", token=token)
    return max((m.get("created_at") or "" for m in listing(data)), default="")


def resolve_chat_uid(token: str, redeem: dict, last_ts=last_message_ts) -> str:
    """The chat this account actually talks in.

    An account keeps every chat a past activation provisioned, most of them
    dead, so pick by the newest message rather than the first listed.
    """
    chat = redeem.get("chat") or {}
    if chat.get("uid"):
        return chat["uid"]
    _, data = api("GET", "/v1/chats", token=token)
    chats = listing(data)
    live = [c for c in chats if c.get("status") == "active" and c.get("uid")] or chats
    if not live:
        raise RuntimeError(f"no chat on this account: {json.dumps(data)[:200]}")
    return max(live, key=lambda c: last_ts(token, c["uid"]))["uid"]


def activation_loop() -> dict:
    """Hold a live code until the owner texts it; codes expire, so re-mint."""
    a = read_json(ACTIVATION, {})
    if not a.get("activation_secret"):
        a = mint_activation()
    while True:
        status, data = api("POST", "/v1/auth/activate/redeem",
                           {"activation_secret": a["activation_secret"]})
        if status == 410 or data.get("status") == "expired":
            log("activation code expired, minting a fresh one")
            a = mint_activation()
        elif data.get("status") == "verified" and data.get("token"):
            token = data["token"]
            creds = {"token": token, "chat_uid": resolve_chat_uid(token, data)}
            write_json(CREDS, creds)
            ACTIVATION.unlink(missing_ok=True)
            log(f"activated: chat {creds['chat_uid']}")
            return creds
        time.sleep(5)


# --- Bridge ---

def list_chats(creds: dict) -> list:
    status, data = api("GET", "/v1/chats", token=creds["token"])
    if status >= 400:
        # A listing hiccup must not cost the owner their own 1:1.
        log(f"chat listing failed {status}: {json.dumps(data)[:160]}")
        return [creds["chat_uid"]]
    return [c["uid"] for c in listing(data) if c.get("uid")] or [creds["chat_uid"]]


def poll_chat(creds: dict, chat_uid: str) -> int:
    """Route new messages in one chat, seeding it silently the first time.

    Seeding is per chat: a group joined months in carries its own history, and
    replaying it would hand the Boss a fake inbox.
    """
    status, data = api("GET", f"/v1/chats/{chat_uid}/messages", token=creds["token"])
    if status >= 400:
        log(f"read failed {chat_uid} {status}: {json.dumps(data)[:160]}")
        return 0
    msgs = listing(data)
    with STATE_LOCK:
        st = read_json(STATE, {})
        seeded = set(st.get("seeded_chats", []))
        first = chat_uid not in seeded
        if first:
            seen = list(st.get("seen_ids", []))
            seen += [m["uid"] for m in msgs if m.get("uid") and m["uid"] not in seen]
            st["seen_ids"] = seen[-1000:]
            st["seeded_chats"] = sorted(seeded | {chat_uid})
            write_json(STATE, st)
    if first:
        log(f"seeded {len(msgs)} existing message(s) in {chat_uid} as already handled")
        return 0
    routed = 0
    for m in sorted(msgs, key=lambda m: m.get("created_at") or ""):
        r = route_message(m)
        if not r.startswith("skip"):
            log(r)
        routed += r.startswith("ROUTE")
    return routed


def bridge_loop(creds: dict):
    log(f"bridge up, routing to {BOSS_AGENT}")
    while True:
        try:
            for uid in list_chats(creds):
                poll_chat(creds, uid)
        except (OSError, ValueError) as e:
            log(f"poll error: {e}")
        time.sleep(POLL_SECONDS)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "reply":
        args = sys.argv[2:]
        chat_uid = args.pop(0) if args and args[0].startswith("cht_") else ""
        if not args:
            raise SystemExit(f"usage: {SELF} reply [cht_x] \"text\"")
        print(json.dumps(send_message(" ".join(args), chat_uid)))
    elif cmd == "status":
        creds, act = read_json(CREDS, {}), read_json(ACTIVATION, {})
        print(json.dumps({
            "activated": bool(creds.get("token")),
            "chat_uid": creds.get("chat_uid"),
            "text_this": f"Plow Activate: {act['display_code']}" if act.get("display_code") else None,
            "to": act.get("send_to"),
            "seeded_chats": read_json(STATE, {}).get("seeded_chats"),
        }, indent=2))
    elif cmd == "serve":
        creds = read_json(CREDS, {})
        bridge_loop(creds if creds.get("token") else activation_loop())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()

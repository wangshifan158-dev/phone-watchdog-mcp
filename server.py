#!/usr/bin/env python3
"""Phone Watchdog MCP Server v2.1

v2.1 changes:
  - send_bark now accepts optional url parameter for iOS Shortcuts deep link
  - send_notification MCP tool accepts optional url parameter

v2 changes:
  - Command ACK: phone confirms execution, AI can verify
  - Command dedup: each command has a unique ID, phone won't execute twice
  - Phone heartbeat: every poll is a heartbeat, server tracks last seen time
  - Watchdog health: single endpoint shows full system status
  - Bark notifications on lock

Endpoints for phone (iOS Shortcuts):
    POST /upload       - Upload screenshot (JPEG body)
    POST /event        - Report app open/close event
    POST /screen-time  - Upload screen time data
    GET  /command      - Poll for pending command (returns command + ID)
    POST /ack          - Confirm command execution {"command_id": "...", "status": "done|failed"}
    GET  /health       - Health check

MCP tools for AI:
    get_screenshot       - Read latest uploaded screenshot
    request_screenshot   - Queue a screenshot command
    get_phone_status     - Current app, last event, screenshot time
    get_app_history      - Recent app open/close log
    get_screen_time      - Today's screen time report
    lock_apps            - Send lock command (Jomo Start Session)
    unlock_apps          - Send unlock command (Jomo Stop Session)
    send_notification    - Push a message via Bark (with optional url for deep link)
    watchdog_health      - Full system health: heartbeat, command status, data freshness

Usage:
    cp .env.example .env
    pip3 install flask requests pillow
    python3 server.py
"""

import base64
import hmac
import json
import os
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests as req
from flask import Flask, request, jsonify
from PIL import Image as PILImage

# --- Config ---

def load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env()

TOKEN = os.environ.get("PHONE_UPLOAD_TOKEN", "").strip()
HOST = os.environ.get("PHONE_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PHONE_MCP_PORT", "18770"))
BARK_URL = os.environ.get("BARK_URL", "").strip().rstrip("/")
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "90"))  # seconds

if not TOKEN:
    print("Error: PHONE_UPLOAD_TOKEN must be set in .env")
    exit(1)

DATA = Path(os.path.dirname(__file__)) / "data"
DATA.mkdir(exist_ok=True)
LATEST_IMG = DATA / "latest.jpg"
STATE_FILE = DATA / "state.json"
LOG_FILE = DATA / "app-usage.jsonl"
COMMAND_FILE = DATA / "command.json"
ACK_FILE = DATA / "ack.json"
SCREEN_TIME_FILE = DATA / "screen-time.json"
HEARTBEAT_FILE = DATA / "heartbeat.json"

app = Flask(__name__)

# --- Helpers ---

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now_ts():
    return time.time()

def check_auth():
    auth = request.headers.get("Authorization", "")
    return hmac.compare_digest(auth, f"Bearer {TOKEN}")

def read_json(path, fallback=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback if fallback is not None else {}

def write_json(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def read_state():
    return read_json(STATE_FILE, {"current_app": None, "updated_at": None})

def update_heartbeat():
    """Record phone poll as heartbeat."""
    write_json(HEARTBEAT_FILE, {"last_seen": now_str(), "last_seen_ts": now_ts()})

def phone_alive():
    """Check if phone has polled recently."""
    hb = read_json(HEARTBEAT_FILE)
    if not hb or "last_seen_ts" not in hb:
        return False, None, None
    elapsed = now_ts() - hb["last_seen_ts"]
    return elapsed < HEARTBEAT_TIMEOUT, hb["last_seen"], round(elapsed)

# --- Phone endpoints ---

@app.route("/health", methods=["GET"])
def health():
    alive, last_seen, elapsed = phone_alive()
    return jsonify({
        "ok": True,
        "time": now_str(),
        "phone_alive": alive,
        "phone_last_seen": last_seen,
        "phone_silence_seconds": elapsed,
    })

@app.route("/upload", methods=["POST"])
def upload_screenshot():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    update_heartbeat()
    body = request.get_data()
    if not body or len(body) > 20 * 1024 * 1024:
        return jsonify({"error": "invalid image"}), 400
    try:
        img = PILImage.open(BytesIO(body))
        img.verify()
        img = PILImage.open(BytesIO(body)).convert("RGB")
        tmp = DATA / "latest.tmp.jpg"
        img.save(tmp, "JPEG", quality=88)
        os.replace(tmp, LATEST_IMG)
    except Exception:
        return jsonify({"error": "invalid image"}), 400
    state = read_state()
    state["screenshot_at"] = now_str()
    write_json(STATE_FILE, state)
    return jsonify({"ok": True, "time": state["screenshot_at"]})

@app.route("/event", methods=["POST"])
def record_event():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    update_heartbeat()
    try:
        d = request.get_json()
        app_name = str(d["app"]).strip()[:100]
        event = str(d.get("event", "open")).strip().lower()
        if not app_name or event not in ("open", "close"):
            raise ValueError
    except Exception:
        return jsonify({"error": "invalid event"}), 400
    ts = now_str()
    state = read_state()
    state.update({
        "current_app": app_name if event == "open" else None,
        "last_app": app_name,
        "last_event": event,
        "updated_at": ts,
    })
    write_json(STATE_FILE, state)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"app": app_name, "event": event, "time": ts}, ensure_ascii=False) + "\n")
    return jsonify({"ok": True, "time": ts})

@app.route("/screen-time", methods=["POST"])
def upload_screen_time():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    update_heartbeat()
    body = request.get_data()
    if not body or len(body) > 64 * 1024:
        return jsonify({"error": "too large"}), 400
    try:
        payload = json.loads(body)
    except Exception:
        payload = {"raw": body.decode("utf-8", errors="replace").strip()}
    record = {"screen_time": payload, "updated_at": now_str()}
    write_json(SCREEN_TIME_FILE, record)
    return jsonify({"ok": True, "time": record["updated_at"]})

@app.route("/command", methods=["GET"])
def get_command():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    update_heartbeat()
    if not COMMAND_FILE.exists():
        return jsonify({"command": "none"})
    data = read_json(COMMAND_FILE, {"command": "none"})
    # Don't delete yet - wait for ACK. Mark as delivered.
    if data.get("command") != "none" and not data.get("delivered"):
        data["delivered"] = True
        data["delivered_at"] = now_str()
        write_json(COMMAND_FILE, data)
    return jsonify(data)

@app.route("/ack", methods=["POST"])
def ack_command():
    """Phone confirms it executed a command."""
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    update_heartbeat()
    try:
        d = request.get_json()
        cmd_id = str(d["command_id"]).strip()
        status = str(d.get("status", "done")).strip().lower()
    except Exception:
        return jsonify({"error": "invalid ack"}), 400

    # Save ACK
    ack = {
        "command_id": cmd_id,
        "status": status,
        "acked_at": now_str(),
    }
    write_json(ACK_FILE, ack)

    # Clear command if ID matches
    cmd = read_json(COMMAND_FILE)
    if cmd.get("id") == cmd_id:
        COMMAND_FILE.unlink(missing_ok=True)

    return jsonify({"ok": True, **ack})

# --- MCP protocol ---

TOOLS = [
    {"name": "get_screenshot", "description": "Read the latest screenshot uploaded by the phone", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "request_screenshot", "description": "Ask the phone to take a screenshot and upload it", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_phone_status", "description": "Current app, last event, latest screenshot time", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_app_history", "description": "Recent app open/close events", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max events to return (default 20)"}}}},
    {"name": "get_screen_time", "description": "Today's screen time report from Jomo", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "lock_apps", "description": "Lock the phone by starting a Jomo session", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "unlock_apps", "description": "Unlock the phone by stopping the Jomo session", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "send_notification", "description": "Push a notification to the phone via Bark. Use url parameter to deep-link into an iOS Shortcut.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "url": {"type": "string", "description": "Optional deep link URL, e.g. shortcuts://run-shortcut?name=YourShortcut"}}, "required": ["body"]}},
    {"name": "watchdog_health", "description": "Full system health: phone heartbeat, pending commands, ACK status, data freshness", "inputSchema": {"type": "object", "properties": {}}},
]

def set_command(cmd):
    """Queue a command with a unique ID for dedup."""
    cmd_id = uuid.uuid4().hex[:12]
    data = {
        "command": cmd,
        "id": cmd_id,
        "created_at": now_str(),
        "created_ts": now_ts(),
        "delivered": False,
    }
    write_json(COMMAND_FILE, data)
    return data

def send_bark(title, body, url=None):
    if not BARK_URL:
        return {"ok": False, "error": "BARK_URL not configured"}
    try:
        params = {"url": url} if url else {}
        r = req.post(f"{BARK_URL}/{title}/{body}", params=params, timeout=10)
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def build_health():
    """Build full system health report."""
    alive, last_seen, elapsed = phone_alive()
    state = read_state()
    cmd = read_json(COMMAND_FILE) if COMMAND_FILE.exists() else None
    ack = read_json(ACK_FILE) if ACK_FILE.exists() else None
    st = read_json(SCREEN_TIME_FILE) if SCREEN_TIME_FILE.exists() else None

    health = {
        "server": {"ok": True, "time": now_str()},
        "phone": {
            "alive": alive,
            "last_seen": last_seen,
            "silence_seconds": elapsed,
            "timeout_threshold": HEARTBEAT_TIMEOUT,
        },
        "screenshot": {
            "exists": LATEST_IMG.exists(),
            "taken_at": state.get("screenshot_at"),
        },
        "current_app": state.get("current_app"),
        "pending_command": None,
        "last_ack": None,
        "screen_time_updated_at": st.get("updated_at") if st else None,
    }

    if cmd and cmd.get("command") != "none":
        age = round(now_ts() - cmd.get("created_ts", now_ts()))
        health["pending_command"] = {
            "command": cmd["command"],
            "id": cmd.get("id"),
            "created_at": cmd.get("created_at"),
            "delivered": cmd.get("delivered", False),
            "age_seconds": age,
            "stuck": age > HEARTBEAT_TIMEOUT and not cmd.get("delivered"),
        }

    if ack:
        health["last_ack"] = ack

    return health

def ok_text(rid, text):
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}}

def ok_image(rid, path):
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "image", "data": b64, "mimeType": "image/jpeg"}]}}

def err_text(rid, text):
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": True}}

def process(d):
    m = d.get("method", "")
    rid = d.get("id")
    p = d.get("params", {})

    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "phone-watchdog-mcp", "version": "2.1.0"},
        }}

    if m == "notifications/initialized":
        return None

    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if m == "tools/call":
        n = p.get("name", "")
        args = p.get("arguments", {})
        try:
            if n == "get_screenshot":
                if not LATEST_IMG.exists():
                    return err_text(rid, "No screenshot uploaded yet")
                return ok_image(rid, LATEST_IMG)

            if n == "request_screenshot":
                r = set_command("screenshot")
                return ok_text(rid, json.dumps({
                    "requested": True,
                    "command_id": r["id"],
                    "created_at": r["created_at"],
                    "note": "Wait for phone to poll and upload. Check with watchdog_health."
                }, ensure_ascii=False))

            if n == "get_phone_status":
                state = read_state()
                alive, last_seen, elapsed = phone_alive()
                state["phone_alive"] = alive
                state["phone_last_seen"] = last_seen
                return ok_text(rid, json.dumps(state, ensure_ascii=False))

            if n == "get_app_history":
                limit = min(max(int(args.get("limit", 20)), 1), 100)
                if not LOG_FILE.exists():
                    return ok_text(rid, "[]")
                lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
                events = []
                for line in lines:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                return ok_text(rid, json.dumps(events, ensure_ascii=False))

            if n == "get_screen_time":
                data = read_json(SCREEN_TIME_FILE)
                if not data:
                    return ok_text(rid, '{"available": false}')
                return ok_text(rid, json.dumps({"available": True, **data}, ensure_ascii=False))

            if n == "lock_apps":
                r = set_command("lock")
                if BARK_URL:
                    send_bark("Phone Locked", "Apps are now locked")
                return ok_text(rid, json.dumps({
                    "sent": True,
                    "command_id": r["id"],
                    "created_at": r["created_at"],
                    "note": "Check watchdog_health for ACK status."
                }, ensure_ascii=False))

            if n == "unlock_apps":
                r = set_command("unlock")
                return ok_text(rid, json.dumps({
                    "sent": True,
                    "command_id": r["id"],
                    "created_at": r["created_at"],
                }, ensure_ascii=False))

            if n == "send_notification":
                title = str(args.get("title", "Watchdog"))
                body = str(args.get("body", ""))
                url = str(args.get("url", "")) or None
                if not body:
                    return err_text(rid, "body is required")
                r = send_bark(title, body, url=url)
                return ok_text(rid, json.dumps(r, ensure_ascii=False))

            if n == "watchdog_health":
                return ok_text(rid, json.dumps(build_health(), ensure_ascii=False))

            return err_text(rid, f"Unknown tool: {n}")
        except Exception as e:
            return err_text(rid, str(e))

    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown method: {m}"}}

@app.route("/mcp", methods=["POST"])
def mcp():
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {TOKEN}"):
        return jsonify({"error": "unauthorized"}), 401
    d = request.get_json()
    if isinstance(d, list):
        results = [r for x in d for r in (process(x),) if r]
        return jsonify(results)
    r = process(d)
    return jsonify(r) if r else ("", 204)

# --- Entry ---

if __name__ == "__main__":
    print(f"Phone Watchdog MCP v2.1 starting on {HOST}:{PORT}")
    print(f"  Data dir: {DATA}")
    print(f"  Bark: {'configured' if BARK_URL else 'not configured'}")
    print(f"  Heartbeat timeout: {HEARTBEAT_TIMEOUT}s")
    print(f"  MCP endpoint: http://{HOST}:{PORT}/mcp")
    print()
    app.run(host=HOST, port=PORT)

"""
server.py — Aria Web App (Flask backend)
Serves the web UI and provides API endpoints for chat (blocking and SSE
streaming), settings, and room data.

The VoiceAssistant is a singleton: built once, reused across requests, and
rebuilt only when settings change. Turns are serialized with its lock.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config as cfg_mod
import tts as tts_mod

app = Flask(__name__, static_folder="static", static_url_path="")
# Static files change during development: never let the browser cache stale UI.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
CORS(app)

# A fresh id each time this process starts. The frontend compares this
# against what it has stored and wipes its local chat history on a
# mismatch, so a relaunched app always starts clean while a plain page
# reload against the same still-running server keeps its history.
SESSION_ID = str(uuid.uuid4())


# ── Assistant singleton ─────────────────────────────────────────────
_assistant = None
_assistant_lock = threading.Lock()


def get_assistant():
    """Return the shared VoiceAssistant, building it on first use."""
    global _assistant
    with _assistant_lock:
        if _assistant is None:
            from assistant import VoiceAssistant
            asst = VoiceAssistant(cfg_mod.load_config())
            asst.initialize()
            _assistant = asst
        return _assistant


def invalidate_assistant():
    """Drop the singleton so the next request rebuilds it (settings changed)."""
    global _assistant
    with _assistant_lock:
        _assistant = None


# ── Rate limiting (no new dependencies) ─────────────────────────────
_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}

CHAT_LIMIT = 20          # chat turns
CHAT_WINDOW = 60.0       # per this many seconds


def _rate_limited(bucket: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(bucket, []) if now - t < CHAT_WINDOW]
        if len(hits) >= CHAT_LIMIT:
            _rate_hits[bucket] = hits
            return True
        hits.append(now)
        _rate_hits[bucket] = hits
        return False


def _rate_response():
    return jsonify({"error": "Slow down a little. Try again in a few seconds."}), 429


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/session", methods=["GET"])
def session_info():
    return jsonify({"session_id": SESSION_ID})


# ── Chat endpoints ──────────────────────────────────────────────────
def _get_message():
    data = request.get_json(silent=True) or {}
    return (data.get("message") or "").strip()


@app.route("/api/chat", methods=["POST"])
def chat():
    """Blocking chat turn. Kept as a fallback for clients without SSE."""
    user_text = _get_message()
    if not user_text:
        return jsonify({"error": "Empty message"}), 400
    if _rate_limited(request.remote_addr or "local"):
        return _rate_response()

    asst = get_assistant()
    if not asst.is_ready:
        return jsonify({"error": "API key not configured",
                        "detail": "Add an API key in Settings."}), 400

    with asst.lock:
        out = asst.respond(user_text)
    return jsonify({
        "message": out.get("message", ""),
        "tool_events": out.get("tool_events", []),
    })


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """SSE chat turn: tokens, tool events, then a final 'done' event."""
    user_text = _get_message()
    if not user_text:
        return jsonify({"error": "Empty message"}), 400
    if _rate_limited(request.remote_addr or "local"):
        return _rate_response()

    asst = get_assistant()
    if not asst.is_ready:
        return jsonify({"error": "API key not configured",
                        "detail": "Add an API key in Settings."}), 400

    def generate():
        with asst.lock:
            try:
                for event in asst.respond_stream(user_text):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as e:  # last-resort boundary: never hang the stream
                err = {"type": "error", "message": f"Server error: {e}"}
                yield f"data: {json.dumps(err)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/chat/reset", methods=["POST"])
def chat_reset():
    """Clear the server-side conversation history."""
    asst = get_assistant()
    with asst.lock:
        asst.reset_history()
    return jsonify({"ok": True})


# ── Settings endpoints ──────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def get_settings():
    cfg = cfg_mod.load_config()
    return jsonify({
        "api_provider": cfg.get("api_provider", ""),
        "api_model": cfg.get("api_model", ""),
        "has_key": bool(cfg.get("api_key")),
        "first_run_complete": cfg.get("first_run_complete", False),
    })


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    cfg = cfg_mod.load_config()
    provider = data.get("api_provider", cfg.get("api_provider", "deepseek"))
    if provider not in cfg_mod.PROVIDERS:
        return jsonify({"error": f"Unknown provider '{provider}'"}), 400
    cfg["api_provider"] = provider
    # Always use the provider's default model: deepseek-v4-flash for both
    # DeepSeek and OpenRouter. The user does not pick a model.
    cfg["api_model"] = cfg_mod.PROVIDERS[provider]["default_model"]
    if data.get("api_key"):
        cfg["api_key"] = data["api_key"]
    cfg["first_run_complete"] = True
    cfg_mod.save_config(cfg)
    invalidate_assistant()
    return jsonify({"ok": True})


@app.route("/api/providers", methods=["GET"])
def list_providers():
    """Single source of truth for the settings dropdown."""
    return jsonify([
        {"id": pid, "label": info.get("label", pid),
         "default_model": info.get("default_model", "")}
        for pid, info in cfg_mod.PROVIDERS.items()
    ])


# ── Area listing ────────────────────────────────────────────────────
@app.route("/api/areas", methods=["GET"])
def list_areas():
    from assistant import tool_list_areas
    return jsonify(json.loads(tool_list_areas()))


# ── Greeting ────────────────────────────────────────────────────────
@app.route("/api/greeting", methods=["GET"])
def greeting():
    asst = get_assistant()
    if not asst.is_ready:
        return jsonify({"message": "Add an API key in Settings to wake Aria up."})
    return jsonify({"message": asst.greeting()})


# ── Text-to-speech (local, free, offline Kokoro voice) ──────────────
@app.route("/api/tts", methods=["POST"])
def synthesize_speech():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:2000]
    if not text:
        return jsonify({"error": "Empty text"}), 400
    if not tts_mod.is_ready():
        return jsonify({"error": "VOICE_NOT_READY",
                        "detail": tts_mod.last_error()
                        or "The offline voice is still downloading (one-time setup)."}), 503
    try:
        wav_bytes = tts_mod.synthesize_wav(text)
    except Exception as e:
        return jsonify({"error": f"Synthesis failed: {e}"}), 500
    return Response(wav_bytes, mimetype="audio/wav")


@app.route("/api/tts/status", methods=["GET"])
def tts_status():
    return jsonify({"ready": tts_mod.is_ready(), "error": tts_mod.last_error()})


@app.route("/api/tts/test", methods=["GET"])
def tts_test():
    """Browser-openable end-to-end check. Navigating to this URL returns the
    actual synthesized audio, so you can hear the real server voice directly,
    bypassing the frontend entirely. If this sounds good but the app sounds
    robotic, the fault is in browser playback, not synthesis."""
    phrase = ("Hi, I'm Aria, your library booking assistant. "
              "This is the offline neural voice.")
    if not tts_mod.is_ready():
        return jsonify({"ready": False, "error": tts_mod.last_error()
                        or "Voice not loaded yet."}), 503
    try:
        wav_bytes = tts_mod.synthesize_wav(phrase)
    except Exception as e:
        return jsonify({"error": f"Synthesis failed: {e}"}), 500
    return Response(wav_bytes, mimetype="audio/wav",
                    headers={"Content-Disposition": "inline; filename=aria_test.wav"})


# ── Auth / SSO login ──────────────────────────────────────────────────
_login_error: str | None = None


@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    import booking_agent as ba
    from mrbs import Mrbs
    cookies = ba.load_cookies()
    if not cookies:
        return jsonify({"logged_in": False, "reason": "no_cookies",
                        "login_error": _login_error})
    try:
        client = Mrbs(cookies=cookies)
        ok = client.is_logged_in()
    except Exception:
        ok = False
    return jsonify({"logged_in": ok, "reason": None if ok else "expired",
                    "login_error": _login_error})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Start the SSO login flow in a background browser window."""
    # Check browser availability before accepting the request
    browser_ok, browser_msg = _check_auth_browser()
    if not browser_ok:
        return jsonify({"ok": False, "error": browser_msg}), 400

    data = request.get_json(silent=True) or {}
    area = str(data.get("area", "8")).strip() or "8"

    global _login_error
    _login_error = None

    def _run():
        global _login_error
        import booking_agent as ba
        try:
            ok = ba.login_noninteractive(area=area)
            if not ok:
                _login_error = (
                    "Sign-in window was closed before completing login. "
                    "Try again and keep the window open until you see "
                    "the HKUST room calendar."
                )
        except Exception as e:
            _login_error = str(e)
            import logging
            logging.getLogger("aria").warning("login flow failed: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "detail": "A browser window opened for login."})


@app.route("/api/auth/browser-check", methods=["GET"])
def auth_browser_check():
    ok, msg = _check_auth_browser()
    return jsonify({"ok": ok, "message": msg})


def _check_auth_browser():
    """Return (available: bool, message: str) for the SSO login browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, (
            "Sign-in browser not installed. Close Aria and run the LAUNCHER "
            "again to finish setting it up."
        )

    # Probe Chromium only. sign_in() in booking_agent.py always launches
    # Chromium, on every platform. Probing WebKit or Firefox as a fallback
    # would report "ready" on a machine where the real sign-in cannot run.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
            return True, "Chromium ready"
    except Exception:
        pass

    return False, (
        "Sign-in browser not installed. Close Aria and run the LAUNCHER "
        "again to finish setting it up."
    )


def _start_background_jobs():
    # Under the Flask/Werkzeug reloader, the launcher process re-execs a
    # child process to run the app; only start the download in that real
    # worker process so we never race two processes downloading the same
    # file into the same path at once.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        tts_mod.ensure_ready_async()


if __name__ == "__main__":
    # Allow rebinding to a port still in TIME_WAIT from a previous run.
    # Essential for the macOS launcher: without this, double-clicking a
    # second time within ~60 s silently fails because the server cannot
    # bind and the background subshell never opens the browser.
    import socketserver
    socketserver.TCPServer.allow_reuse_address = True

    # Debug OFF unless deliberately asked for. It used to default ON, which
    # enabled the Werkzeug interactive debugger: a browser console that
    # executes arbitrary Python in this process on any unhandled exception.
    # Aria holds the user's HKUST session cookie, so that console is the
    # whole account.
    debug = os.environ.get("ARIA_DEBUG", "0") not in ("0", "false", "no")
    app.debug = debug
    _start_background_jobs()
    print("Aria Web App — http://localhost:8091")

    # 127.0.0.1, NOT 0.0.0.0.
    #
    # 0.0.0.0 binds every network interface, so on shared Wi-Fi (campus,
    # halls, a café) anyone on the same network could open this student's
    # Aria at http://<their-laptop-ip>:8091 and use it. Verified: from a
    # second machine on the LAN the page and /api/session both answered 200.
    # Since Aria is signed in to HKUST as the user, a stranger could book
    # rooms in their name, and with the old debug default could also reach
    # the debugger console.
    #
    # Nothing needs the wider bind: the launcher opens localhost on this
    # same machine. Set ARIA_HOST=0.0.0.0 deliberately if you ever want to
    # reach it from your phone, and understand what you are opening.
    host = os.environ.get("ARIA_HOST", "127.0.0.1")
    app.run(host=host, port=8091, debug=debug, threaded=True)

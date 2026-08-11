#!/usr/bin/env python3
"""Local web control panel for the Whatnot radar.

    python web.py            # then open http://127.0.0.1:8765

Binds whatever config.panel_host says, loopback by default. To reach it from
your phone, put the machine on a private network (e.g. Tailscale) and use the
panel's "allow my phone" button, or --host 0.0.0.0 for a single run. Never
expose it to the open internet: it can start a browser and read your config.
"""

import argparse
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    from pydantic import BaseModel
except ModuleNotFoundError as exc:  # nearly always: venv not activated
    _venv = Path(__file__).resolve().parent / ".venv"
    _py = _venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    print(f"\n  Missing '{exc.name}' — the setup packages aren't loaded.\n")
    if _py.exists():
        print("  You're using the wrong Python. Run this instead:\n")
        print(f"      {_py} web.py\n")
    else:
        print("  It looks like setup hasn't finished. From this folder run:\n")
        print("      python3 -m venv .venv")
        print("      .venv/bin/pip install -r requirements.txt")
        print("      .venv/bin/playwright install chromium\n")
    sys.exit(1)

import control
import profile_tools

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
EXAMPLE_PATH = PROJECT_DIR / "config.example.json"
STATIC = PROJECT_DIR / "static"

# Only these may be written from the UI, so a stray field can't inject config.
SERVE_HOST = "127.0.0.1"   # set from --host at startup

EDITABLE = {
    "my_username", "notifier", "bark_key", "ntfy_topic", "ntfy_server",
    "seller_poll_seconds", "giveaway_poll_seconds", "max_concurrent_streams",
    "pinned_extra_tabs", "watch_giveaways", "headless", "minimize_browser",
    "sellers", "blacklist", "blacklist_temp", "bought_sellers", "foreign_sellers",
    "discovery", "panel_password", "panel_host",
}

# Phase 5: presets. Only the Pokémon feedId is one we have actually captured
# and verified; the country filter is a plain value swap on the same field, so
# offering the country list is safe. Other categories need their own capture —
# see the README rather than guessing an id here.
COUNTRIES = {"NL": "Netherlands", "GB": "United Kingdom", "DE": "Germany",
             "BE": "Belgium", "FR": "France", "US": "United States"}
CATEGORY_PRESETS = {
    "Pokémon cards": "CATEGORY_FEED_V2:TGl2ZXN0cmVhbVRhZ05vZGU6OTA3",
}

app = FastAPI(title="Whatnot Radar")

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def ensure_password() -> str:
    """One random password per install, created on first run. Never shipped in
    the repo — it lives in the user's own gitignored config."""
    cfg = load_config()
    pw = cfg.get("panel_password") or ""
    if not pw:
        import secrets
        pw = "-".join(secrets.token_hex(2) for _ in range(3))  # xxxx-xxxx-xxxx
        cfg["panel_password"] = pw
        save_config(cfg)
    return pw


@app.middleware("http")
async def guard(request, call_next):
    """No password when you're already on the machine; required from anywhere
    else. Keeps first-run frictionless while making --host 0.0.0.0 safe on an
    untrusted network."""
    if (request.client.host if request.client else "") in LOOPBACK:
        return await call_next(request)
    import base64
    import hmac
    header = request.headers.get("authorization", "")
    supplied = ""
    if header.startswith("Basic "):
        try:
            supplied = base64.b64decode(header[6:]).decode().split(":", 1)[-1]
        except Exception:
            supplied = ""
    if not hmac.compare_digest(supplied, ensure_password()):
        return JSONResponse(
            {"error": "password required — see the 'use on your phone' card"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Whatnot Radar"'})
    return await call_next(request)


def load_config() -> dict:
    # encoding pinned on purpose: the file is written as UTF-8 and holds
    # accented names ("Pokémon cards…"). Left to the locale, a Windows panel
    # reads it back as cp1252 and the category no longer matches its preset.
    if not CONFIG_PATH.exists():
        return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def require_stopped() -> None:
    if control.read_pid() is not None:
        raise HTTPException(409, "Stop the radar first — the browser profile "
                                 "is in use while it's running.")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def readiness() -> dict:
    """The three things that must be done before starting is worth doing.

    Kept server-side so the Start button and the setup checklist can't drift
    apart, and so a start over SSH or from the phone is checked the same way.
    """
    cfg = load_config()
    topic = str(cfg.get("ntfy_topic", ""))
    alerts = (bool(cfg.get("bark_key")) if cfg.get("notifier", "bark") == "bark"
              else bool(topic) and "CHANGE-ME" not in topic)
    watching = bool((cfg.get("discovery") or {}).get("sources")) or bool(cfg.get("sellers"))
    steps = [
        {"id": "notifier", "label": "phone alerts", "done": alerts,
         "why": "nothing can reach your phone without it"},
        {"id": "login", "label": "whatnot login", "done":
            profile_tools.session_state()["logged_in"],
         "why": "logged out, giveaways you are shown may not be enterable"},
        {"id": "watch", "label": "what to watch", "done": watching,
         "why": "no category or seller means nothing to watch"},
    ]
    return {"steps": steps, "ready": all(s["done"] for s in steps),
            "missing": [s["label"] for s in steps if not s["done"]]}


def login_running() -> bool:
    """A login is in progress only while its helper process is actually alive —
    a leftover marker from a crashed helper must not strand the panel showing
    an "I'm done" button forever."""
    marker = PROJECT_DIR / ".login_running"
    try:
        pid = int(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    import psutil
    if psutil.pid_exists(pid):
        return True
    marker.unlink(missing_ok=True)
    return False


@app.get("/api/status")
def api_status():
    st = control.status(log_lines=1)
    result = PROJECT_DIR / ".login_result"
    return {
        "control": st,
        "config_exists": CONFIG_PATH.exists(),
        "session": profile_tools.session_state(),
        "profile": profile_tools.sizes(),
        "login_in_progress": login_running(),
        "login_result": result.read_text(encoding="utf-8") if result.exists() else "",
        "readiness": readiness(),
    }


@app.get("/api/log")
def api_log(lines: int = 200):
    if not control.LOG_FILE.exists():
        return {"lines": []}
    return {"lines": control.LOG_FILE.read_text(
        encoding="utf-8", errors="replace").splitlines()[-lines:]}


class StartReq(BaseModel):
    force: bool = False


@app.post("/api/start")
def api_start(req: StartReq):
    if not req.force:
        state = readiness()
        if not state["ready"]:
            raise HTTPException(409, "not set up yet — still to do: "
                                     + ", ".join(state["missing"])
                                     + ". Finish those, or use “start anyway”.")
    return {"message": control.start(force=req.force)}


@app.post("/api/stop")
def api_stop():
    return {"message": control.stop()}


@app.post("/api/restart")
def api_restart():
    return {"message": control.restart()}


def _hint(secret: str) -> str:
    """Enough to recognise a saved value, not enough to use it."""
    secret = str(secret or "")
    return f"••••{secret[-4:]}" if len(secret) >= 4 else ""


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    # Never ship secrets to the browser; send only 'is it set' + a short hint
    # so the UI can show what's saved without exposing the value.
    redacted = dict(cfg)
    bark, topic = cfg.get("bark_key", ""), str(cfg.get("ntfy_topic", ""))
    redacted["bark_key_set"] = bool(bark)
    redacted["bark_key_hint"] = _hint(bark) if bark else ""
    topic_set = bool(topic) and "CHANGE-ME" not in topic
    redacted["ntfy_topic_set"] = topic_set
    # no hint for the untouched placeholder — it would look like a saved value
    redacted["ntfy_topic_hint"] = _hint(topic) if topic_set else ""
    redacted.pop("bark_key", None)
    redacted.pop("ntfy_topic", None)
    # the panel password has its own endpoint (loopback-only); it must never
    # ride along in a general config fetch
    redacted.pop("panel_password", None)
    return redacted


def ssh_server_state() -> dict:
    """Is anything listening on port 22 here, and if not, how to fix it.

    "Run Script Over SSH could not connect to the SSH server" is the same
    message whether the address is wrong, the tailnet is down, or — much more
    commonly on Windows — no SSH server was ever installed. Checking locally
    rules the last one in or out before the user starts guessing.
    """
    import socket as _socket
    listening = False
    for addr in ("127.0.0.1", "::1"):
        try:
            with _socket.create_connection((addr, 22), timeout=1):
                listening = True
                break
        except OSError:
            continue
    system = platform.system()
    if system == "Windows":
        how = {
            "summary": "Windows does not install an SSH server by default.",
            "steps": ["Open PowerShell as Administrator (right-click the Start "
                      "button → Terminal (Admin)), then run these three lines."],
            "commands": [
                "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0",
                "Start-Service sshd",
                "Set-Service -Name sshd -StartupType Automatic",
            ],
            "where": "Settings → System → Optional features → OpenSSH Server",
            "check": "In PowerShell run  Get-Service sshd  — Status should say "
                     "Running. Your Windows sign-in name and password are what "
                     "the shortcut asks for.",
        }
    elif system == "Darwin":
        how = {
            "summary": "macOS has an SSH server built in — it only needs "
                       "switching on.",
            "steps": ["System Settings → General → Sharing → turn on "
                      "Remote Login.",
                      "Click the ⓘ beside it and make sure your own user is "
                      "in the allowed list."],
            "commands": [],
            "where": "System Settings → General → Sharing → Remote Login",
            "check": "System Settings → General → Sharing → Remote Login shows "
                     "a green dot. Your Mac login name and password are what "
                     "the shortcut asks for.",
        }
    else:
        how = {
            "summary": "Install and enable OpenSSH.",
            "steps": ["On Debian/Ubuntu:"],
            "commands": ["sudo apt install openssh-server",
                         "sudo systemctl enable --now ssh"],
            "where": "the openssh-server package",
            "check": "systemctl status ssh  — it should say active (running).",
        }
    return {"listening": listening, **how}


@app.get("/api/ssh-info")
def api_ssh_info():
    """Ready-made remote-control commands, so the panel can show exactly what
    to put in a phone SSH shortcut instead of the user assembling it.

    Two shapes, because they are not interchangeable. A terminal wants one
    `ssh user@host '<command>'` line. Apple's "Run Script Over SSH" action has
    separate Host / Port / User / Script fields and runs the Script *as* the
    remote command — paste the full ssh line in there and it tries to run ssh
    on the far end. So the bare remote command is published separately.
    """
    import getpass
    import shlex
    import socket

    user = getpass.getuser()
    hosts = []
    short = socket.gethostname().split(".")[0]
    if platform.system() == "Darwin":
        hosts.append(f"{short}.local")
    else:
        hosts.append(short)
    # The tailnet name works from anywhere, the local one only on this network,
    # so prefer it when there is one.
    if (name := _tailnet_host()):
        hosts.insert(0, name)

    actions = ("start", "stop", "status")
    parts = [sys.executable, str(PROJECT_DIR / "control.py")]
    if platform.system() == "Windows":
        # Windows sshd hands the command to cmd.exe, which understands double
        # quotes and not POSIX single quotes — and these paths contain spaces
        # often enough (Program Files, "My Documents") to matter.
        quoted = " ".join(f'"{p}"' for p in parts)
    else:
        quoted = " ".join(shlex.quote(p) for p in parts)
    scripts = {action: f"{quoted} {action}" for action in actions}

    def outer(script: str) -> str:
        """Quote the remote command for the LOCAL shell, readably.

        shlex.quote is always correct but renders a path containing a space as
        ''"'"'/My Stuff/python'"'"' …' — correct, and nobody will trust it
        enough to paste it. Prefer plain quotes when they are unambiguous.
        """
        if "'" not in script:
            return f"'{script}'"
        if not any(c in script for c in '"$`\\'):
            return f'"{script}"'
        return shlex.quote(script)

    return {
        "user": user, "hosts": hosts, "host": hosts[0], "port": 22,
        # SSH does not need the QR code — that is only a shortcut for opening
        # the panel in a phone browser. Say which address these use, because a
        # local name silently fails the moment you leave the house.
        "via": "tailscale" if name else "local network",
        "scripts": scripts,
        "ssh_server": ssh_server_state(),
        "commands": {action: f"ssh {user}@{hosts[0]} {outer(script)}"
                     for action, script in scripts.items()},
        "remote_login_hint": (
            "Enable System Settings → General → Sharing → Remote Login"
            if platform.system() == "Darwin" else
            "Install the OpenSSH Server optional feature, then start the sshd service"
            if platform.system() == "Windows" else
            "Make sure an SSH server (openssh-server) is installed and running"),
    }


@app.post("/api/config")
def api_set_config(patch: dict):
    if unknown := set(patch) - EDITABLE:
        raise HTTPException(400, f"not editable: {sorted(unknown)}")
    cfg = load_config()
    cfg.update(patch)
    save_config(cfg)
    return {"message": "saved — restart the radar to apply",
            "restart_needed": control.read_pid() is not None}


class SellerReq(BaseModel):
    seller: str
    list_name: str = "blacklist"   # blacklist | sellers | bought_sellers | foreign_sellers
    remove: bool = False


@app.post("/api/seller")
def api_seller(req: SellerReq):
    if req.list_name not in {"blacklist", "sellers", "bought_sellers",
                             "foreign_sellers"}:
        raise HTTPException(400, "unknown list")
    cfg = load_config()
    items = [s for s in cfg.get(req.list_name, [])]
    name = req.seller.strip().lower()
    if not name:
        raise HTTPException(400, "empty seller name")
    if req.remove:
        items = [s for s in items if s.strip().lower() != name]
    elif name not in [s.strip().lower() for s in items]:
        items.append(name)
    cfg[req.list_name] = items
    save_config(cfg)
    return {req.list_name: items,
            "restart_needed": control.read_pid() is not None}


@app.post("/api/test-notification")
def api_test():
    cfg = load_config()
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        import monitor
        monitor.make_notifier(cfg).send(
            "✅ Whatnot radar test ✅", "Tap to open Whatnot.",
            "https://www.whatnot.com", priority="high")
    except SystemExit as exc:      # make_notifier exits on bad config
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"send failed: {exc.__class__.__name__}: {exc}")
    return {"message": "sent — check your phone"}


@app.post("/api/login/start")
def api_login_start():
    require_stopped()
    marker = PROJECT_DIR / ".login_running"
    (PROJECT_DIR / ".login_done").unlink(missing_ok=True)
    (PROJECT_DIR / ".login_result").unlink(missing_ok=True)
    proc = subprocess.Popen([sys.executable, str(PROJECT_DIR / "browser_task.py"),
                             "login"], cwd=str(PROJECT_DIR),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    marker.write_text(str(proc.pid))
    return {"message": "browser opening — log in, then click \"I'm done\""}


@app.post("/api/login/finish")
def api_login_finish():
    # The marker stays until browser_task actually exits: it still has to close
    # Chrome and count the cookies, and clearing it here would report "done"
    # seconds before we know whether anything was saved.
    (PROJECT_DIR / ".login_done").touch()
    return {"message": "closing browser and saving session…"}


@app.post("/api/clear-cache")
def api_clear_cache():
    require_stopped()
    freed = profile_tools.clear_cache()
    return {"message": f"cleared {freed / 1e6:.0f} MB of cache — still logged in"}


@app.post("/api/clear-site-data")
def api_clear_site_data():
    require_stopped()
    freed = profile_tools.clear_site_data()
    return {"message": f"cleared {freed / 1e6:.1f} MB of site data — "
                       "you are now logged out, run Login again"}


def find_tailscale() -> str | None:
    """The tailscale CLI.

    which() alone is not enough: the Windows installer puts tailscale.exe in
    Program Files without adding it to PATH, and the macOS App Store build
    hides it inside the .app. Both look like "Tailscale isn't installed" to a
    bare which(), which is why a working tailnet showed no QR code.
    """
    import shutil as _shutil
    if (found := _shutil.which("tailscale")):
        return found
    candidates = [
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
        "/usr/bin/tailscale",
    ]
    if (local := os.environ.get("LOCALAPPDATA")):
        candidates.append(str(Path(local) / "Tailscale" / "tailscale.exe"))
    for path in candidates:
        try:
            if Path(path).is_file():
                return path
        except OSError:
            continue
    return None


def tailnet_state() -> dict:
    """Tailnet name plus why it isn't usable, so the panel can tell "install
    it" apart from "it's installed, just sign in"."""
    ts = find_tailscale()
    if not ts:
        return {"host": None, "state": "missing"}
    try:
        out = subprocess.run([ts, "status", "--json"], capture_output=True,
                             text=True, timeout=6)
        me = (json.loads(out.stdout) or {}).get("Self") or {}
        host = (me.get("DNSName") or "").rstrip(".")
        if not me.get("Online") or not host:
            return {"host": None, "state": "signed-out"}
        return {"host": host, "state": "ready"}
    except Exception:
        return {"host": None, "state": "signed-out"}


def _tailnet_host() -> str | None:
    return tailnet_state()["host"]


@app.get("/api/phone-info")
def api_phone_info(request: Request):
    """Everything needed to open the panel on a phone, resolved for THIS
    machine — so nobody has to work out their own hostname."""
    local = (request.client.host if request.client else "") in LOOPBACK
    tailnet = tailnet_state()
    host = tailnet["host"]
    port = request.url.port or 8765
    bound_all = SERVE_HOST != "127.0.0.1"
    wants_all = str(load_config().get("panel_host") or "127.0.0.1") != "127.0.0.1"
    return {
        "tailscale": bool(host),
        "tailscale_state": tailnet["state"],
        "url": f"http://{host}:{port}" if host else None,
        # only echo the password to someone already sitting at the machine
        "password": ensure_password() if local else None,
        "bound_all": bound_all,
        # saved but not in effect yet: the panel has to be started again
        "restart_pending": wants_all and not bound_all,
        "launcher": "start-panel.bat" if platform.system() == "Windows"
                    else "start-panel.command",
        "manual_command": f'"{sys.executable}" web.py --host 0.0.0.0',
        "project_dir": str(PROJECT_DIR),
        "port": port,
    }


class PhoneAccessReq(BaseModel):
    allow: bool


@app.post("/api/phone-access")
def api_phone_access(req: PhoneAccessReq):
    """Save whether the panel should accept connections from other devices.

    A saved setting rather than a command-line flag because the launcher is how
    this actually gets started, and it passes no arguments.
    """
    cfg = load_config()
    cfg["panel_host"] = "0.0.0.0" if req.allow else "127.0.0.1"
    save_config(cfg)
    if not req.allow:
        return {"message": "phone access off — restart the panel to apply"}
    return {"message": "phone access saved — close the panel window and start "
                       "it again to apply"}


@app.get("/api/phone-qr.svg")
def api_phone_qr(request: Request):
    try:
        import qrcode
        import qrcode.image.svg
    except ModuleNotFoundError:
        # Missing optional dep shouldn't 500 the panel — the URL is shown too.
        raise HTTPException(503, "QR support not installed (pip install qrcode)")
    host = _tailnet_host()
    if not host:
        raise HTTPException(404, "Tailscale not running")
    q = qrcode.QRCode(box_size=9, border=2)
    q.add_data(f"http://{host}:{request.url.port or 8765}")
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/panel-password/regenerate")
def api_regen_password(request: Request):
    if (request.client.host if request.client else "") not in LOOPBACK:
        raise HTTPException(403, "Only from the computer itself")
    cfg = load_config()
    cfg.pop("panel_password", None)
    save_config(cfg)
    return {"message": f"new password: {ensure_password()}"}


@app.post("/api/reset-profile")
def api_reset_profile():
    require_stopped()
    freed = profile_tools.reset_profile()
    return {"message": f"profile reset ({freed / 1e6:.0f} MB removed) — "
                       "click Log in to sign in again"}


@app.get("/api/presets")
def api_presets():
    return {"countries": COUNTRIES, "categories": CATEGORY_PRESETS}


class SourceReq(BaseModel):
    category: str
    countries: list[str]


@app.post("/api/discovery-source")
def api_discovery_source(req: SourceReq):
    feed_id = CATEGORY_PRESETS.get(req.category)
    if not feed_id:
        raise HTTPException(400, "unknown category preset")
    bad = [c for c in req.countries if c not in COUNTRIES]
    if bad or not req.countries:
        raise HTTPException(400, f"pick at least one known country (bad: {bad})")
    cfg = load_config()
    cfg.setdefault("discovery", {})
    cfg["discovery"]["enabled"] = True
    cfg["discovery"]["sources"] = [{
        "name": f"{req.category} shipped from {'/'.join(req.countries)}",
        "feedId": feed_id,
        "filters": [{"field": "userCountry.keyword", "values": req.countries}],
    }]
    save_config(cfg)
    return {"discovery": cfg["discovery"],
            "restart_needed": control.read_pid() is not None}


@app.exception_handler(HTTPException)
def http_error(_request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def open_panel(url: str, prefer_app_window: bool = True) -> None:
    """Open the panel in a window of its own, away from the radar's tabs.

    webbrowser.open asks the OS to hand the URL to the running browser. On
    macOS that is an AppleEvent addressed to the *application*, and the radar's
    Chrome — same app bundle, different profile — answers it, so the panel
    opens as a tab in among the stream tabs.

    Launching the Chrome binary ourselves avoids that: Chrome's single-instance
    routing is keyed on user-data-dir, so with none given this reaches the
    user's ordinary profile rather than the radar's. --app then gives a
    standalone window with no tab strip for anything to be adopted into.
    """
    if prefer_app_window:
        from browser_task import find_google_chrome
        if (chrome := find_google_chrome()):
            try:
                subprocess.Popen([chrome, f"--app={url}"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass          # fall through to the ordinary browser
    webbrowser.open(url)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=None,
                    help="override the saved panel_host for this run")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open the panel automatically")
    ap.add_argument("--tab", action="store_true",
                    help="open the panel as an ordinary browser tab instead of "
                         "its own window")
    args = ap.parse_args()
    # Nobody types a command to start this — they double-click the launcher,
    # which passes no arguments. So the phone-access choice has to live in the
    # config where the launcher will pick it up, not in a flag.
    host = args.host or str(load_config().get("panel_host") or "127.0.0.1")
    global SERVE_HOST
    SERVE_HOST = host
    args.host = host
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"\n  Reachable on your network ({args.host}).", flush=True)
        print(f"  Password for remote access: {ensure_password()}", flush=True)
        print("  Only do this on a private network such as Tailscale — never "
              "the open internet.", flush=True)

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Whatnot Radar panel:  {url}", flush=True)
    print("  Leave this window open. Press Ctrl+C to shut the panel down.\n", flush=True)
    if not args.no_browser:
        # Opening it for them: "nothing happened" is otherwise the most common
        # first-run confusion — the server starts but never shows anything.
        threading.Thread(
            target=lambda: (time.sleep(1.5), open_panel(url, not args.tab)),
            daemon=True).start()

    serve(args.host, args.port)


def serve(host: str, port: int) -> None:
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                           log_level="warning"))
    if platform.system() != "Windows":
        server.run()
        _farewell()
        return

    # Windows only: the proactor event loop wakes for socket I/O, not for
    # signals, so an idle panel swallows Ctrl+C completely and the window can
    # only be closed by killing it. Take the signal ourselves and run a
    # heartbeat that gives the loop a reason to wake up and see the flag.
    import asyncio
    import signal

    server.install_signal_handlers = lambda: None

    async def run() -> None:
        def stop(*_):
            server.should_exit = True
        for sig in (signal.SIGINT, signal.SIGBREAK):
            signal.signal(sig, stop)
        task = asyncio.ensure_future(server.serve())
        while not task.done():
            await asyncio.sleep(0.2)
        await task

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    _farewell()


def _farewell() -> None:
    still_running = control.read_pid() is not None
    print("\n  Panel stopped."
          + ("  The radar is still running in the background — "
             "'python control.py stop' ends it." if still_running else "")
          + "\n", flush=True)


if __name__ == "__main__":
    main()

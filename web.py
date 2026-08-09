#!/usr/bin/env python3
"""Local web control panel for the Whatnot radar.

    python web.py            # then open http://127.0.0.1:8765

Binds to loopback only. To reach it from your phone, put the machine on a
private network (e.g. Tailscale) and use --host 0.0.0.0 — never expose it to
the open internet: it can start a browser and read your config.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import control
import profile_tools

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
EXAMPLE_PATH = PROJECT_DIR / "config.example.json"
STATIC = PROJECT_DIR / "static"

# Only these may be written from the UI, so a stray field can't inject config.
EDITABLE = {
    "my_username", "notifier", "bark_key", "ntfy_topic", "ntfy_server",
    "seller_poll_seconds", "giveaway_poll_seconds", "max_concurrent_streams",
    "pinned_extra_tabs", "watch_giveaways", "headless", "sellers",
    "blacklist", "blacklist_temp", "bought_sellers", "foreign_sellers",
    "discovery",
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


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return json.loads(EXAMPLE_PATH.read_text())
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    tmp.replace(CONFIG_PATH)


def require_stopped() -> None:
    if control.read_pid() is not None:
        raise HTTPException(409, "Stop the radar first — the browser profile "
                                 "is in use while it's running.")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def api_status():
    st = control.status(log_lines=1)
    return {
        "control": st,
        "config_exists": CONFIG_PATH.exists(),
        "session": profile_tools.session_state(),
        "profile": profile_tools.sizes(),
        "login_in_progress": (PROJECT_DIR / ".login_running").exists(),
    }


@app.get("/api/log")
def api_log(lines: int = 200):
    if not control.LOG_FILE.exists():
        return {"lines": []}
    return {"lines": control.LOG_FILE.read_text(errors="replace").splitlines()[-lines:]}


class StartReq(BaseModel):
    force: bool = False


@app.post("/api/start")
def api_start(req: StartReq):
    return {"message": control.start(force=req.force)}


@app.post("/api/stop")
def api_stop():
    return {"message": control.stop()}


@app.post("/api/restart")
def api_restart():
    return {"message": control.restart()}


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    # Never ship secrets to the browser; report only whether they're set.
    redacted = dict(cfg)
    redacted["bark_key_set"] = bool(cfg.get("bark_key"))
    redacted["ntfy_topic_set"] = bool(cfg.get("ntfy_topic")) and \
        "CHANGE-ME" not in str(cfg.get("ntfy_topic", ""))
    redacted.pop("bark_key", None)
    redacted.pop("ntfy_topic", None)
    return redacted


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
    proc = subprocess.Popen([sys.executable, str(PROJECT_DIR / "browser_task.py"),
                             "login"], cwd=str(PROJECT_DIR),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    marker.write_text(str(proc.pid))
    return {"message": "browser opening — log in, then click \"I'm done\""}


@app.post("/api/login/finish")
def api_login_finish():
    (PROJECT_DIR / ".login_done").touch()
    (PROJECT_DIR / ".login_running").unlink(missing_ok=True)
    return {"message": "closing browser and saving session"}


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"WARNING: binding {args.host} — only do this on a private "
              "network such as Tailscale.")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Whatnot Giveaway Radar — detects giveaways in Whatnot livestreams and pushes
a phone notification. Detection/notification only: giveaway entry is manual,
always, per Whatnot's rules.

Subcommands:
  login   open a headful browser to log in to Whatnot (session persists)
  test    send a test notification to verify the phone-side setup
  run     the long-running watch loop
"""

import argparse
import json
import random
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

HTTP = requests.Session()  # connection reuse across notifications

# Windows consoles and files default to the legacy code page (cp1252), which
# cannot encode the emoji in notification titles — a 🎁 then raises
# UnicodeEncodeError and looks like a failed send. Force UTF-8 everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # already-wrapped or non-tty stream
        pass

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
CONFIG_EXAMPLE_PATH = PROJECT_DIR / "config.example.json"
STATE_PATH = PROJECT_DIR / "state.json"
PROFILE_DIR = PROJECT_DIR / "whatnot-profile"
PROFILE_BACKUP_DIR = PROJECT_DIR / "whatnot-profile-backup"
DISCOVERY_QUERY_PATH = PROJECT_DIR / "discovery_getfeed.graphql"
SEND_LOG_PATH = PROJECT_DIR / "notifications.log"


def find_google_chrome() -> str | None:
    """Return the real Chrome executable if present, else None."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/google-chrome",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return str(path)
    for command in ("google-chrome", "google-chrome-stable", "chrome"):
        try:
            import shutil as _shutil
            resolved = _shutil.which(command)
            if resolved:
                return resolved
        except Exception:
            pass
    return None


def build_browser_launch_kwargs(user_data_dir: str | Path, *, headless: bool = False, extra_args=None):
    """Use the real Chrome executable and persistent profile without unsupported sandbox flags."""
    kwargs = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "chromium_sandbox": True,
    }
    chrome_path = find_google_chrome()
    if chrome_path:
        kwargs["executable_path"] = chrome_path
    if extra_args:
        kwargs["args"] = list(extra_args)
    return kwargs


# Anything further off-screen than this is not a window a person is using.
# Windows parks minimised windows at -32000,-32000, which is a free, very
# high-confidence bot signal for anyone reading window.screenX from JS.
OFFSCREEN_LIMIT = -10000


def normalise_window(ctx):
    """Report the browser window's real position, and drag it back on screen.

    The radar never minimises or resizes the window itself — but Chrome has
    been observed starting minimised on Windows on its own, either launched
    that way or restoring a stored placement. That matters beyond tidiness: a
    window at -32000,-32000 is visible to page JavaScript through
    screenX/screenY, and stream tabs loaded in that state were served
    Cloudflare challenges while the plain GraphQL discovery call was fine.

    Corrective only, and it logs what it found either way — the state the radar
    actually starts in should not have to be guessed at.
    """
    page = ctx.pages[0] if ctx.pages else None
    if page is None:
        return "no page to inspect"
    cdp = None
    try:
        cdp = ctx.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        b = cdp.send("Browser.getWindowBounds", {"windowId": window_id})["bounds"]
        found = (f"{b.get('width')}x{b.get('height')} at "
                 f"{b.get('left')},{b.get('top')} ({b.get('windowState')})")
        offscreen = (b.get("left", 0) < OFFSCREEN_LIMIT
                     or b.get("top", 0) < OFFSCREEN_LIMIT)
        if b.get("windowState") == "normal" and not offscreen:
            return f"window {found}"
        _set_window_state(cdp, window_id, "normal")
        if offscreen:
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id,
                      "bounds": {"left": 40, "top": 40,
                                 "width": b.get("width") or 1280,
                                 "height": b.get("height") or 800}})
        after = cdp.send("Browser.getWindowBounds",
                         {"windowId": window_id})["bounds"]
        return (f"window was {found} — moved on screen to "
                f"{after.get('left')},{after.get('top')} "
                f"({after.get('windowState')})")
    except Exception as exc:
        return f"could not read the window ({exc.__class__.__name__})"
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass


def _set_window_state(cdp, window_id, state: str, timeout: float = 4.0) -> bool:
    """Ask for a window state and wait until it has actually taken effect.

    setWindowBounds returns before the window manager has finished — macOS
    animates de-miniaturisation, and an immediate read still says "minimized".
    Without the wait, a window we believed we had restored stayed hidden.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": state}})
            current = cdp.send("Browser.getWindowBounds",
                               {"windowId": window_id})["bounds"]["windowState"]
        except Exception:
            return False
        if current == state:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def backup_profile(source_dir: Path = PROFILE_DIR, backup_dir: Path = PROFILE_BACKUP_DIR) -> bool:
    """Copy the current profile to a last-known-good backup."""
    if not source_dir.exists():
        return False
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    shutil.copytree(source_dir, backup_dir)
    return True


def restore_profile(source_dir: Path = PROFILE_DIR, backup_dir: Path = PROFILE_BACKUP_DIR) -> bool:
    """Restore the last-known-good profile after a Cloudflare challenge."""
    if not backup_dir.exists():
        return False
    if source_dir.exists():
        shutil.rmtree(source_dir, ignore_errors=True)
    shutil.copytree(backup_dir, source_dir)
    return True


def audit_send(backend: str, priority: str, title: str, outcome: str) -> None:
    """One line per notification attempt — the evidence trail for comparing
    what was sent against what the phone actually displayed."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(SEND_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{stamp} | {backend} | {priority} | {outcome} | {title}\n")
    except (OSError, UnicodeError) as exc:
        # The audit trail is a convenience. If it can't be written the push has
        # already gone out, so this must never surface as a failed send — that
        # would make the caller retry a notification the phone already has.
        log(f"could not write {SEND_LOG_PATH.name}: {exc.__class__.__name__}")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            "config.json not found. Create it with:\n"
            f"  cp {CONFIG_EXAMPLE_PATH.name} {CONFIG_PATH.name}\n"
            "then set ntfy_topic to a long random string (topics are public!) "
            "and subscribe to it in the ntfy app on your phone."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)


def remove_bought_seller(seller: str) -> bool:
    """Drop a seller from config.json's bought_sellers (case-insensitive), so a
    stale manual buyer-eligibility flag doesn't re-arm on restart. Returns True
    if the config changed."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    current = cfg.get("bought_sellers", [])
    kept = [s for s in current if s.strip().lower() != seller.strip().lower()]
    if len(kept) == len(current):
        return False
    cfg["bought_sellers"] = kept
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    tmp.replace(CONFIG_PATH)
    return True


def prune_state(state: dict, days: int = 7) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    for key in ("notified_streams", "notified_giveaways", "notified_wins",
                "seen_streams", "seller_giveaway_seen", "bought_streams"):
        entries = state.get(key, {})
        for k in list(entries):
            try:
                at = datetime.fromisoformat(entries[k]["at"]).timestamp()
            except (KeyError, ValueError):
                continue
            if at < cutoff:
                del entries[k]


# --- Notifiers ---------------------------------------------------------------


class NtfyNotifier:
    # JSON publish endpoint instead of per-message headers: HTTP headers are
    # latin-1 only, which breaks emoji in titles (🔴, 🎁).
    PRIORITY_TO_INT = {"min": 1, "low": 2, "default": 3, "high": 4,
                       "urgent": 5, "max": 5, "critical": 5}

    def __init__(self, config: dict):
        self.server = config.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        self.topic = config["ntfy_topic"]
        if not self.topic or "CHANGE-ME" in self.topic:
            sys.exit("Set ntfy_topic in config.json to a long random string first.")

    def send(self, title, message, click_url, priority="high", tags=None,
             group=None, sound=None):
        payload = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "click": click_url,
            "priority": self.PRIORITY_TO_INT.get(priority, 4),
        }
        if tags:
            payload["tags"] = tags
        try:
            resp = HTTP.post(self.server, json=payload, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            audit_send("ntfy", priority, title, f"FAILED {e.__class__.__name__}")
            raise
        msg_id = ""
        try:
            msg_id = resp.json().get("id", "")
        except ValueError:
            pass
        audit_send("ntfy", priority, title, f"OK id={msg_id}")


class BarkNotifier:
    # "critical" would break through silent mode WITH sound — user prefers
    # vibration-only on silent, so max maps to timeSensitive (buzzes on
    # silent via iOS "Always Play" haptics, sound only when unmuted).
    # "critical" breaks through silent/Focus WITH sound — used for wins and
    # eligible buyers-giveaways. Requires iOS Critical Alerts permission on Bark;
    # without it these deliver nothing. Regular giveaways use timeSensitive (buzz).
    PRIORITY_TO_LEVEL = {
        "critical": "critical",
        "max": "timeSensitive",
        "urgent": "timeSensitive",
        "high": "timeSensitive",
        "default": "active",
        "low": "passive",
        "min": "passive",
    }

    def __init__(self, config: dict):
        self.key = config.get("bark_key", "")
        if not self.key:
            sys.exit('notifier is "bark" but bark_key is empty in config.json.')

    def send(self, title, message, click_url, priority="high", tags=None,
             group=None, sound=None):
        url = "https://api.day.app/{}/{}/{}".format(
            self.key,
            urllib.parse.quote(title, safe=""),
            urllib.parse.quote(message, safe=""),
        )
        params = {
            "url": click_url,
            "level": self.PRIORITY_TO_LEVEL.get(priority, "active"),
        }
        # No "call" mode anywhere: it rings/buzzes for ~30s, which the user
        # found obnoxious. Normal single vibration for the max tier.
        if params["level"] == "critical":
            # Critical already breaks through silent with sound; no call (which
            # rings for 30s) — a single critical tone like Bark's preview.
            params["volume"] = "5"  # critical-alert ringtone volume (0-10)
        if sound:
            params["sound"] = sound  # distinct loud tone for extreme alerts
        if group:
            params["group"] = group  # stacks notifications per category on iOS
        try:
            resp = HTTP.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            audit_send("bark", priority, title, f"FAILED {e.__class__.__name__}")
            raise
        audit_send("bark", priority, title, "OK")


def make_notifier(config: dict):
    backend = config.get("notifier", "ntfy")
    if backend == "ntfy":
        return NtfyNotifier(config)
    if backend == "bark":
        return BarkNotifier(config)
    sys.exit(f'Unknown notifier "{backend}" in config.json (expected "ntfy" or "bark").')


def send_notification(config, title, message, click_url, priority="high", tags=None):
    make_notifier(config).send(title, message, click_url, priority=priority, tags=tags)


# --- Live detection (Phase 3) ------------------------------------------------
#
# Findings from investigating /user/<seller> on 2026-07-07 (two live sellers,
# en-GB locale, logged in):
# - The profile's show list renders as anchors with href="/live/<uuid>?...".
#   The currently-live show's anchor text is "Live · <viewer count>"; scheduled
#   shows instead say "Tomorrow 20:00", "Thu 14:00", etc. A sibling anchor with
#   the same href carries the stream title.
# - No usable JSON/GraphQL network responses observed — the page data is
#   server-rendered, so we read the DOM (text-based, not class-based).
# - Headless Chromium gets stuck on a Cloudflare interstitial; headful passes.

BASE_URL = "https://www.whatnot.com"


def jitter(seconds: float) -> float:
    return seconds * random.uniform(0.8, 1.2)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_logged_out(page) -> bool:
    locator = page.get_by_role("link", name="Log in").or_(
        page.get_by_role("button", name="Log in")
    )
    return locator.count() > 0


def is_bot_challenge(page) -> bool:
    # Cloudflare's interstitial reliably sets the title; a scoped text locator
    # avoids serializing the whole DOM (page.content()) on every call.
    if any(m in page.title() for m in ("Just a moment", "Attention Required")):
        return True
    try:
        return page.get_by_text("security verification").count() > 0
    except Exception:
        return False


def check_seller_live(page, username: str):
    """Return (stream_url, stream_id, title) if the seller is live, else None."""
    page.goto(f"{BASE_URL}/user/{username}", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    links = page.eval_on_selector_all(
        "a[href*='/live/']",
        "els => els.map(e => ({href: e.getAttribute('href'), text: (e.innerText || '').trim()}))",
    )
    live_href = None
    for l in links:
        if re.match(r"^Live\b", l["text"]):
            live_href = l["href"]
            break
    if not live_href:
        return None
    title = next(
        (l["text"] for l in links
         if l["href"] == live_href and not re.match(r"^Live\b", l["text"])),
        "",
    )
    stream_id = live_href.split("/live/")[1].split("?")[0]
    return f"{BASE_URL}/live/{stream_id}", stream_id, title


# --- Giveaway detection (Phase 4) --------------------------------------------
#
# Findings from watching a live TCG stream during a real giveaway (2026-07-08):
# - The stream page opens Phoenix channels over
#   wss://www.whatnot.com/services/{live,auction,chat}/socket/websocket.
#   Frames are JSON arrays: [join_ref, ref, topic, event, payload].
# - On topic "auction:<stream_id>":
#     giveaway_started              {id: <uuid>, product: {...}, ...}
#     giveaway_entered              {giveaway: {productId: <same uuid>, entryCount}}
#     giveaway_entry_count_updated  {entryCount, productId}
# - The user_joined payload (sent on connect) has "activeGiveaway": non-null
#   if a giveaway is running when we join — catches mid-giveaway tab opens.
# - livestream_update payloads carry "status": "PLAYING" / "ENDED".
# - DOM fallback: the pinned section shows text "Giveaway with <N> entries".
# - When the winner is drawn (observed live on two draws, 2026-07-08), a
#   "giveaway_won" event arrives on the auction topic. The win is modeled as a
#   €0 sale: winner = payload.product.purchaserUser.username, giveaway id =
#   payload.giveaway.productId (== product.id), title = product.name.
#   payload.giveawayEntries lists all entrant usernames.

GIVEAWAY_REMINDER = "Enter in the app."

# Prizes not worth a slot or a buzz (extend with | as needed, e.g. "sticker|coaster").
LOW_VALUE_TITLE_RE = re.compile(r"sticker", re.I)

# What we actually want to win. Packs skip the anti-collision deferral and are
# alerted the moment the frame lands — not to win a seat race (entries are NOT
# capped; see LIST_TRUNCATION) but because giveaways run only ~5 minutes, so
# every second of delay is a second less to notice and enter.
PACK_TITLE_RE = re.compile(r"pack|box|etb|blister|booster|bundle|tin", re.I)

# The giveawayEntries NAME list is truncated at 50; the giveaway's entryCount
# is the real total (measured: counter=105 alongside a 50-name list). So seeing
# your name proves you entered, but not seeing it proves nothing. Entries are
# NOT capped — anyone can enter, and popular pack pools run well past 50.
LIST_TRUNCATION = 50


class StreamWatcher:
    """A muted tab on a live stream, collecting giveaway events from the WS."""

    def __init__(self, ctx, seller: str, stream_url: str, stream_id: str,
                 source: str = "seller", my_username: str = "",
                 is_foreign: bool = False):
        self.seller = seller
        self.stream_url = stream_url
        self.stream_id = stream_id
        self.source = source  # "seller" (configured) or "discovery"
        self.my_username = my_username  # lowercased; for detecting my purchases
        # Seller ships from another country: their "domestic only" giveaways
        # are restricted to THEIR country, so we can't enter them.
        self.is_foreign = is_foreign
        self.opened_at = time.monotonic()
        self.pending = []
        self.wins = []  # {"username", "giveaway_id"} from GIVEAWAY_WON events
        self.purchases = []  # timestamps where I bought (drained like wins)
        self.joined_at_ms = time.time() * 1000
        # While a giveaway is running, the tab must survive rotation/eviction
        # so the draw is observed (giveaways run ≤5 min; bounded hold).
        self.giveaway_hold_until = 0.0
        self.saw_giveaway = False  # any giveaway activity since the tab opened
        self.last_dom_check = 0.0  # rate-limits the DOM giveaway fallback
        self.entry_peak = {}       # productId -> max entryCount seen (cap probe)
        self.ended = False
        # CDP background target: opens the tab without activating the browser
        # window, so rotation doesn't pull the desktop over. Fallback to
        # new_page() (focus-stealing but functional) if the CDP path breaks.
        try:
            with ctx.expect_page() as pinfo:
                cdp = ctx.new_cdp_session(ctx.pages[0])
                cdp.send("Target.createTarget",
                         {"url": "about:blank", "background": True})
                cdp.detach()
            self.page = pinfo.value
        except Exception as exc:
            # Say so. This is the one path that steals focus — on Windows with
            # the radar on a second virtual desktop it drags you across — and
            # silently falling back left that looking like an unexplained
            # desktop switch with nothing in the log to blame.
            log(f"{seller}: background tab failed ({exc.__class__.__name__}), "
                "opening a normal tab — this one takes focus")
            self.page = ctx.new_page()
        # Watchers only need the WebSocket, but a viewer that fetches ZERO
        # video is a bot signature. So mimic a real tab-switcher: play video
        # for the first few seconds, then stop pulling segments (a backgrounded
        # HLS player keeps polling the small manifest but stops buffering).
        # Saves most of the bandwidth without the "never any video" tell.
        VIDEO_SECONDS = 8

        def _route_video(route):
            req_url = route.request.url
            is_segment = re.search(r"\.ts(\?|$)|\.m4s(\?|$)|\.mp4(\?|$)", req_url, re.I)
            if is_segment and time.monotonic() - self.opened_at > VIDEO_SECONDS:
                route.abort()
            else:
                route.continue_()

        self.page.route(
            re.compile(r"\.m3u8(\?|$)|\.ts(\?|$)|\.m4s(\?|$)|\.mp4(\?|$)", re.I),
            _route_video,
        )
        self.page.on("websocket", self._on_websocket)
        self.page.goto(stream_url, wait_until="domcontentloaded")

    def _on_websocket(self, ws):
        if "/services/auction/socket" in ws.url or "/services/live/socket" in ws.url:
            ws.on("framereceived", self._on_frame)

    def _on_frame(self, payload):
        # Runs inside Playwright's event dispatch — must never raise.
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", "replace")
            # Hot path: a busy stream floods this with chat/bid/presence frames.
            # Skip the json.loads unless the frame could be one we act on.
            # "iveaway" matches giveaway_*/activeGiveaway; "ENDED" matches the
            # livestream_update end signal; my_username matches my own purchase
            # frame (purchaserUser.username). UPDATE THIS if a new handled event
            # is added below without one of these substrings.
            if ("iveaway" not in payload and "ENDED" not in payload
                    and not (self.my_username and self.my_username in payload.lower())):
                return
            msg = json.loads(payload)
            if not (isinstance(msg, list) and len(msg) == 5
                    and isinstance(msg[4], dict)):
                return
            _, _, _, event, data = msg
            if event == "giveaway_started":
                self._queue_giveaway(data.get("id"), data.get("product"))
            elif event == "user_joined" and isinstance(data.get("activeGiveaway"), dict):
                g = data["activeGiveaway"]
                self._queue_giveaway(g.get("id") or g.get("productId"),
                                     g.get("product"))
            elif event == "giveaway_entry_count_updated":
                # Authoritative server-side counter. If this ever exceeds the
                # 50-long giveawayEntries list, then 50 is a LIST TRUNCATION,
                # not an entry cap — which would mean seats aren't limited and
                # our "entered=NO" readings on 50-entry draws are unreliable.
                pid = data.get("productId")
                cnt = data.get("entryCount")
                if pid and isinstance(cnt, int):
                    self.entry_peak[pid] = max(self.entry_peak.get(pid, 0), cnt)
            elif event == "giveaway_won":
                product = data.get("product") or {}
                winner = (product.get("purchaserUser") or {}).get("username") or ""
                gid = str((data.get("giveaway") or {}).get("productId")
                          or product.get("id") or "")
                if winner and gid:
                    self.wins.append({
                        "username": winner,
                        "giveaway_id": gid,
                        "title": product.get("name") or "",
                        "entries": [str(e) for e in
                                    (data.get("giveawayEntries") or [])],
                        # server counter vs list length: distinguishes a real
                        # 50-entry cap from a truncated entrant list
                        "entry_peak": max(self.entry_peak.get(gid, 0),
                                          (data.get("giveaway") or {})
                                          .get("entryCount") or 0),
                        # a BAG draw consumes the purchase that qualified us
                        "buyers_only": bool((product.get("giveaway") or {})
                                            .get("buyerAppreciation")),
                    })
                    self.entry_peak.pop(gid, None)
                # short grace so the win is drained before the slot frees up
                self.giveaway_hold_until = time.monotonic() + 30
            elif event in ("product_sold", "product_updated") and self.my_username:
                # Detect MY purchase → this show's buyers-giveaways become
                # enterable. A real buy (not a giveaway win): I'm the purchaser
                # and it's a paid transaction.
                product = data.get("product") or {}
                buyer = (product.get("purchaserUser") or {}).get("username") or ""
                paid = (product.get("transactionType") not in (None, "GIVEAWAY")
                        or (product.get("soldPriceCents") or 0) > 0)
                if buyer.lower() == self.my_username and paid:
                    self.purchases.append(time.monotonic())
            elif event == "livestream_update" and data.get("status") == "ENDED":
                self.ended = True
        except Exception:
            pass

    def _queue_giveaway(self, gid, product):
        product = product or {}
        gw = product.get("giveaway") or {}
        end_ms = gw.get("giveawayEndTime")
        buyers_only = bool(gw.get("buyerAppreciation"))
        # "Domestic only" = the seller's country. Un-enterable only when the
        # seller ships from somewhere other than where we are.
        unenterable_domestic = bool(gw.get("onlyDomestic")) and self.is_foreign
        title = product.get("name") or product.get("subtitle") or ""
        low_value = bool(LOW_VALUE_TITLE_RE.search(title))
        self.pending.append({
            "id": str(gid or ""),
            "title": title,
            "buyers_only": buyers_only,
            "low_value": low_value,
            "unenterable_domestic": unenterable_domestic,
            "is_pack": bool(PACK_TITLE_RE.search(title)) and not low_value,
            "detected_at": time.monotonic(),  # for alert-latency instrumentation
            "followers_only": bool(gw.get("onlyFollowers")),
            "ends_in": (end_ms / 1000 - time.time()) if end_ms else None,
        })
        # Suppressed kinds (buyers-only, low-value prizes, foreign
        # domestic-only) earn neither the rotation hold nor giveaway-activity
        # status — the slot stays free to idle-rotate toward giveaways the user
        # can actually enter.
        if buyers_only or low_value or unenterable_domestic:
            return
        self.saw_giveaway = True
        # Hold until the known draw time plus buffer (sellers may draw a
        # little late); fall back to 6 min when the end time is absent.
        ends_in = self.pending[-1]["ends_in"]
        hold = min(max(60.0, ends_in + 90.0), 420.0) if ends_in is not None else 360.0
        self.giveaway_hold_until = max(
            self.giveaway_hold_until, time.monotonic() + hold)

    def drain(self):
        """Return queued giveaway events. The brief wait gives Playwright a
        chance to dispatch buffered WS callbacks (they only run during calls)."""
        try:
            self.page.wait_for_timeout(50)
        except Exception:
            self.ended = True
        events, self.pending = self.pending, []
        return events

    def dom_shows_giveaway(self) -> bool:
        try:
            return self.page.get_by_text(
                re.compile(r"Giveaway with \d+ entr", re.I)
            ).count() > 0
        except Exception:
            return False

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass


# --- Discovery (Phase 3b) ----------------------------------------------------
#
# Findings (2026-07-08): the browse/tag page (e.g. /tag/pokemon_cards) loads its
# grid via a GetFeed GraphQL POST. Selecting a country under the "Shipped From"
# filter does NOT change the URL; it refires GetFeed with
#   variables.feedId  = "CATEGORY_FEED_V2:<base64 LivestreamTagNode:...>"
#   variables.filters = [{"field": "userCountry.keyword", "values": ["NL", ...]}]
# The response has data.feed.objects.edges[].node(__typename=FeedEntity).object
# with: id (stream uuid), status ("PLAYING"=live now, "CREATED"=scheduled),
# title, user.username, activeViewers. We replay that exact request from inside
# the page (same session/cookies); the captured query text lives in
# discovery_getfeed.graphql and the per-source variables in config
# discovery.sources, so categories/countries are editable without code changes.

DISCOVERY_FETCH_JS = """async ({query, variables}) => {
    const r = await fetch('/services/graphql/?operationName=GetFeed&ssr=0', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({operationName: 'GetFeed', variables, query}),
    });
    return {status: r.status, body: await r.text()};
}"""


def discover_streams(page, source: dict, query: str):
    """Return [(stream_url, stream_id, seller_username, title)] live now."""
    variables = {
        "withFilterAndSortOptions": False,
        "withListingLivestreamTime": False,
        "withListingBreak": False,
        "withListingVariants": False,
        "withTagRefinements": False,
        "feedId": source["feedId"],
        "filters": source.get("filters", []),
        "objectCursor": None,
        "objectSize": 24,
        "sort": None,
    }
    result = page.evaluate(DISCOVERY_FETCH_JS, {"query": query, "variables": variables})
    if result["status"] != 200:
        raise RuntimeError(f"GetFeed HTTP {result['status']}")
    body = json.loads(result["body"])
    if body.get("errors"):
        raise RuntimeError(f"GetFeed errors: {str(body['errors'])[:200]}")
    edges = body["data"]["feed"]["objects"]["edges"]
    streams = []
    for edge in edges:
        node = edge.get("node") or {}
        if node.get("__typename") != "FeedEntity":
            continue
        obj = node.get("object") or {}
        if obj.get("status") != "PLAYING":
            continue
        sid = obj.get("id")
        user = obj.get("user") or {}
        if not sid:
            continue
        streams.append((
            f"{BASE_URL}/live/{sid}", sid,
            user.get("username") or "?", obj.get("title") or "",
        ))
    return streams


# --- Subcommands -------------------------------------------------------------


def cmd_test(config: dict) -> None:
    print(f"Sending test notification via {config.get('notifier', 'ntfy')}...")
    send_notification(
        config,
        title="✅ Whatnot radar test ✅",
        message="Tap to open Whatnot.",
        click_url="https://www.whatnot.com",
        priority="high",
    )
    print("Sent. Check your phone — tapping the notification should open Whatnot.")


def cmd_login(config: dict) -> None:
    from playwright.sync_api import sync_playwright

    if PROFILE_DIR.exists():
        backup_profile()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            **build_browser_launch_kwargs(PROFILE_DIR, headless=False)
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.whatnot.com/login")
        print("A browser window is open. Log in to Whatnot there.")
        input("When you're logged in, press Enter here to save the session and close... ")
        backup_profile()
        try:
            ctx.close()
        except Exception:
            pass  # window already closed by hand is fine; profile is on disk
    print(f"Session saved in {PROFILE_DIR.name}/. All commands reuse this profile.")


def cmd_run(config: dict) -> None:
    import signal

    from playwright.sync_api import sync_playwright

    # Graceful shutdown via a flag checked between operations. Raising
    # KeyboardInterrupt from the handler mid-Playwright-call corrupts the sync
    # connection and ctx.close() hangs forever; a flag lets the current call
    # finish first. Also re-arms SIGINT, which shells ignore in background
    # children, and routes SIGTERM through the same path.
    stop = {"requested": False}

    def _request_shutdown(signum, frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    def snooze(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not stop["requested"] and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    sellers = config.get("sellers", [])
    blacklist = {s.strip().lower() for s in config.get("blacklist", [])}
    # Temporary blocks: {seller_lower: ISO expiry}. Auto-lift when expired.
    temp_blacklist = {}
    for s, exp in (config.get("blacklist_temp", {}) or {}).items():
        try:
            temp_blacklist[s.strip().lower()] = datetime.fromisoformat(exp)
        except (ValueError, TypeError):
            pass

    def is_blocked(seller: str) -> bool:
        s = seller.strip().lower()
        if s in blacklist:
            return True
        exp = temp_blacklist.get(s)
        return exp is not None and datetime.now(timezone.utc) < exp

    discovery_cfg = config.get("discovery", {}) or {}
    discovery_on = bool(discovery_cfg.get("enabled"))
    if not sellers and not discovery_on:
        sys.exit("No sellers in config.json and discovery is disabled.")
    seller_poll = max(60, config.get("seller_poll_seconds", 90))
    giveaway_poll = max(8, config.get("giveaway_poll_seconds", 10))
    watch_giveaways = config.get("watch_giveaways", True)
    max_streams = config.get("max_concurrent_streams", 3)
    # Extra tabs granted on top of the discovery cap, one per LIVE pinned
    # seller (bounded) — keeps pinned sellers from consuming discovery slots.
    pinned_extra_tabs = config.get("pinned_extra_tabs", 0)
    # Sellers who ship from another country: their "domestic only" giveaways
    # are restricted to their country, so those alerts are useless to us.
    # (Discovery is country-filtered, so anything it finds is already local.)
    foreign_sellers = {s.strip().lower() for s in config.get("foreign_sellers", [])}
    my_username = (config.get("my_username") or "").strip().lower()
    manual_bought = {s.strip().lower() for s in config.get("bought_sellers", [])}
    disc_poll = max(60, discovery_cfg.get("poll_seconds", 300))
    disc_sources = discovery_cfg.get("sources", [])
    disc_notify_new = bool(discovery_cfg.get("notify_new_streams"))
    disc_query = None
    if discovery_on:
        if not disc_sources:
            sys.exit("discovery.enabled is true but discovery.sources is empty.")
        if not DISCOVERY_QUERY_PATH.exists():
            sys.exit(f"{DISCOVERY_QUERY_PATH.name} is missing — re-capture the "
                     "GetFeed query (see discovery comment block).")
        disc_query = DISCOVERY_QUERY_PATH.read_text(encoding="utf-8")

    notifier = make_notifier(config)
    state = load_state()
    state.setdefault("notified_streams", {})
    state.setdefault("notified_giveaways", {})
    state.setdefault("notified_wins", {})
    state.setdefault("seen_streams", {})
    # Per-seller "last seen running an enterable giveaway" — persists across
    # streams and restarts; feeds the weighted rotation below.
    state.setdefault("seller_giveaway_seen", {})
    # Streams we've bought in — persisted, because buyers-giveaway eligibility
    # must survive a monitor restart (it lasts the whole show, and we restart
    # for every config change).
    state.setdefault("bought_streams", {})
    # Seed/refresh from the giveaway history already in state.
    for info in state["notified_giveaways"].values():
        seller, at = info.get("seller"), info.get("at")
        if seller and at and not info.get("suppressed"):
            if at > state["seller_giveaway_seen"].get(seller, {}).get("at", ""):
                state["seller_giveaway_seen"][seller] = {"at": at}
    prune_state(state)

    ROTATE_SECONDS = 600       # slot lifetime for streams that produce giveaways
    IDLE_ROTATE_SECONDS = 300  # rotate early when zero giveaway activity seen
    FAST_TICK_SECONDS = 2      # WS drain cadence while tabs are open (local only)

    # iOS suppresses banners for pushes arriving in rapid succession, and
    # draw-of-#N + start-of-#N+1 land together when sellers chain giveaways.
    # Loud (max) sends record their time; routine 🎲 results are deferred
    # until the loud banner has had the stage to itself.
    LOUD_GAP_SECONDS = 20
    last_loud_send = {"t": -1e9}
    deferred_results = []    # queued 🎲 sends: (title, body, url, state_key, seller)
    # Alert-to-outcome instrumentation: gid -> {latency, is_pack, at}. At draw
    # time we log whether we made the entrant list and whether the pool capped,
    # which is the evidence for whether alert speed is costing us pack seats.
    alerted_races = {}
    deferred_giveaways = []  # 🎁 sends waiting out the gap:
                             # (seller, url, gid, gtitle, followers_only, ends_at)

    def notify_giveaway(seller: str, stream_url: str, gid: str, gtitle: str,
                        buyers_only: bool = False, followers_only: bool = False,
                        ends_in=None, low_value: bool = False,
                        buyers_eligible: bool = False, is_pack: bool = False,
                        detected_at=None, unenterable_domestic: bool = False) -> None:
        if gid in state["notified_giveaways"]:
            return
        if unenterable_domestic:
            # Seller restricted it to their own country and they're not in ours.
            log(f"{seller}: domestic-only giveaway skipped (can't enter from "
                f"abroad) — {gtitle or 'untitled'}")
            state["notified_giveaways"][gid] = {
                "seller": seller, "suppressed": "domestic_only",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)
            return
        if low_value:
            log(f"{seller}: low-value giveaway skipped — {gtitle or 'untitled'}")
            state["notified_giveaways"][gid] = {
                "seller": seller, "suppressed": "low_value",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)
            return
        if buyers_only and not buyers_eligible:
            # Purchase required to enter and I haven't bought here — suppress.
            log(f"{seller}: buyers-only giveaway skipped — {gtitle or 'untitled'}")
            state["notified_giveaways"][gid] = {
                "seller": seller, "suppressed": "buyers_only",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)
            return
        if buyers_only and buyers_eligible:
            # I bought in this show → the BAG is enterable. EXTREME alert.
            log(f"{seller}: 🛒 BUYERS GIVEAWAY (eligible) — {gtitle or 'untitled'}"
                " -> notifying")
            try:
                notifier.send(
                    f"🛒🔔 Buyers giveaway — {seller} 🔔🛒",
                    (f"{gtitle}\n" if gtitle else "")
                    + "You bought here — enter in the app!",
                    # Critical (sound through silent/Focus, standard tone) —
                    # reserved for buyers-giveaways only: rare, and eligibility
                    # was paid for, so these must not be missed.
                    stream_url, priority="critical", group="giveaways",
                )
                last_loud_send["t"] = time.monotonic()
            except Exception as e:
                log(f"{seller}: buyers-giveaway notification failed "
                    f"({e.__class__.__name__}: {e}) — will retry next tick")
                return
            state["notified_giveaways"][gid] = {
                "seller": seller,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)
            return
        # Space loud sends apart: two 🎁 within seconds make iOS mute the
        # second one's banner/vibration. The runner-up waits out the gap.
        # EXCEPTION: packs are what we're here for and giveaways last only ~5
        # minutes — send immediately and accept the banner-collision risk.
        if (not is_pack
                and time.monotonic() - last_loud_send["t"] < LOUD_GAP_SECONDS):
            ends_at = (time.monotonic() + ends_in) if ends_in is not None else None
            log(f"{seller}: GIVEAWAY — {gtitle or 'untitled'} "
                f"(deferred {LOUD_GAP_SECONDS}s: too close to previous alert)")
            deferred_giveaways.append(
                (seller, stream_url, gid, gtitle, followers_only, ends_at))
            return
        lat = (time.monotonic() - detected_at) if detected_at else None
        log(f"{seller}: {'PACK ' if is_pack else ''}GIVEAWAY — "
            f"{gtitle or 'untitled'} -> notifying"
            + (f" (alert latency {lat:.1f}s)" if lat is not None else ""))
        title = f"🎁 Giveaway — {seller} 🎁"
        reminder = ("Follow the seller, then enter." if followers_only
                    else GIVEAWAY_REMINDER)
        if ends_in is not None and ends_in < 60:
            reminder = f"⏳ ~{max(0, int(ends_in))}s left — be quick! " + reminder
        body = (f"{gtitle}\n" if gtitle else "") + reminder
        try:
            notifier.send(title, body, stream_url, priority="max", group="giveaways")
            last_loud_send["t"] = time.monotonic()
        except Exception as e:
            log(f"{seller}: giveaway notification failed "
                f"({e.__class__.__name__}: {e}) — will retry next tick")
            return
        state["notified_giveaways"][gid] = {
            "seller": seller,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state)
        # Remember the alert so the draw can tell us whether we made the race.
        alerted_races[gid] = {"latency": lat, "is_pack": is_pack,
                              "at": time.monotonic()}

    def notify_win(seller: str, stream_url: str, gid: str, gtitle: str) -> None:
        if gid in state["notified_wins"]:
            return
        log(f"{seller}: 🏆 WE WON — {gtitle or gid} -> notifying")
        try:
            notifier.send(
                "🏆 You WON! 🏆",
                f"{gtitle or f'Giveaway in {seller}’s stream'} — check the app.",
                stream_url, priority="max", group="results",
            )
            last_loud_send["t"] = time.monotonic()
        except Exception as e:
            log(f"{seller}: win notification failed "
                f"({e.__class__.__name__}: {e}) — will retry next tick")
            return
        state["notified_wins"][gid] = {
            "seller": seller,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state)

    log(f"Watching {len(sellers)} sellers every ~{seller_poll}s"
        + (f" + discovery ({len(disc_sources)} sources) every ~{disc_poll}s"
           if discovery_on else "")
        + (f"; giveaway checks every ~{giveaway_poll}s, "
           f"tabs = 40% of discovery pool (3..{max_streams})"
           f" +{pinned_extra_tabs}/live pinned seller"
           if watch_giveaways else "; giveaway watching OFF"))
    with sync_playwright() as p:
        if PROFILE_DIR.exists():
            backup_profile()
        ctx = p.chromium.launch_persistent_context(
            **build_browser_launch_kwargs(
                PROFILE_DIR,
                headless=config.get("headless", False),
                extra_args=["--mute-audio"],
            )
        )
        poll_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # Always say where the window actually is, even when we did nothing to
        # it: "it started minimised on its own" is otherwise unfalsifiable.
        log(normalise_window(ctx))
        watchers = {}          # stream_id -> StreamWatcher
        seller_live = {}       # stream_id -> (seller, url, title), from seller polls
        discovered = {}        # stream_id -> (seller, url, title), from discovery
        last_watch_start = {}  # stream_id -> monotonic ts (rotation fairness)
        last_ws_giveaway = {}  # stream_id -> monotonic ts of last WS event
        # Streams whose WS said ENDED stay blocked from reopening for a while:
        # the profile page and discovery feed keep listing a dead stream as
        # live for a minute or two, which otherwise causes open/close churn.
        recently_ended = {}    # stream_id -> monotonic ts
        # stream_id -> monotonic ts of my detected purchase. Seeded from state
        # so a restart mid-show doesn't lose paid-for buyers eligibility.
        bought_streams = {sid: time.monotonic()
                          for sid in state["bought_streams"]}
        if bought_streams:
            log(f"restored buyer-eligibility for {len(bought_streams)} stream(s): "
                + ", ".join(sorted(v.get("seller", "?")
                                   for v in state["bought_streams"].values())))
        manual_seen_live = set()  # manual-bought sellers observed live this run
        # Pinned sellers that can't be resolved from the (country-filtered)
        # discovery feed need a profile page load each poll. Backing off while
        # they're offline turns ~40 loads/hour/seller into ~6, which is the
        # single biggest reduction in mechanical traffic we emit.
        seller_next_check = {}    # seller -> monotonic ts of next allowed check
        seller_offline_streak = {}
        OFFLINE_BACKOFF_MAX = 600
        ENDED_COOLDOWN = 900
        if manual_bought:
            log("manual buyer-eligibility armed for: "
                + ", ".join(sorted(manual_bought)))
        next_seller_poll = 0.0
        next_disc_poll = 0.0

        def check_session(page) -> bool:
            """True if the monitor must stop (bot challenge / logged out)."""
            if is_bot_challenge(page):
                log("Cloudflare challenge detected — restoring the last known-good profile and stopping.")
                restore_profile()
                print("Cloudflare challenge detected. The last known-good profile was restored. Run `python monitor.py login` again before restarting.", flush=True)
                try:
                    notifier.send(
                        "⚠️ Radar stopped: Cloudflare challenge ⚠️",
                        "Cloudflare challenge detected. The last known-good profile was restored. Run monitor.py login again to refresh the session.",
                        BASE_URL, priority="high",
                    )
                except Exception:
                    pass
                return True
            if is_logged_out(page):
                log("Logged out — run `monitor.py login` again. Stopping.")
                try:
                    notifier.send(
                        "⚠️ Re-login needed ⚠️",
                        "Session expired. Run: monitor.py login",
                        BASE_URL, priority="high",
                    )
                except Exception:
                    pass
                return True
            return False

        def open_watcher(sid, seller, url, source):
            log(f"{seller}: opening watch tab ({source})")
            try:
                watchers[sid] = StreamWatcher(
                    ctx, seller, url, sid, source, my_username=my_username,
                    is_foreign=seller.strip().lower() in foreign_sellers)
                last_watch_start[sid] = time.monotonic()
            except Exception as e:
                log(f"{seller}: failed to open stream tab "
                    f"({e.__class__.__name__}: {e})")

        def effective_cap() -> int:
            # Discovery share: ~40% of the discovered pool (3..max_streams), so
            # we scale up at peak without being parked in most of the
            # ecosystem's viewer lists at once.
            disc_cap = min(max_streams, max(3, round(len(discovered) * 0.4)))
            # Pinned sellers get their own headroom on top, so a pinned seller
            # never eats a discovery slot — and costs nothing while offline
            # (seller_live is empty then).
            bonus = min(len(seller_live), pinned_extra_tabs)
            # Same for streams we've bought into: they hold a tab for the rest
            # of the show without displacing discovery rotation.
            bonus += len([s for s in bought_streams if s in discovered])
            return disc_cap + bonus

        def manage_watchers():
            if not watch_giveaways:
                return
            cap = effective_cap()
            for sid, w in list(watchers.items()):
                gone = (
                    (w.source == "seller" and sid not in seller_live)
                    or (w.source == "discovery"
                        and sid not in discovered and sid not in seller_live)
                )
                # A stale listing must not close a tab mid-giveaway; only the
                # stream's own ENDED signal overrides the hold.
                if gone and not w.ended and time.monotonic() < w.giveaway_hold_until:
                    continue
                if w.ended or gone:
                    log(f"{w.seller}: closing tab "
                        f"({'stream ended' if w.ended else 'no longer listed'})")
                    if w.ended:
                        recently_ended[sid] = time.monotonic()
                        bought_streams.pop(sid, None)  # per-show buyer flag ends
                        if state["bought_streams"].pop(sid, None):
                            save_state(state)
                    w.close()
                    del watchers[sid]
            def blocked(sid):
                return time.monotonic() - recently_ended.get(sid, -1e9) < ENDED_COOLDOWN

            # Configured sellers claim slots first, evicting discovery tabs.
            for sid, (seller, url, title) in seller_live.items():
                if sid in watchers or blocked(sid):
                    continue
                if len(watchers) >= cap:
                    disc_ws = [w for w in watchers.values()
                               if w.source == "discovery"
                               and time.monotonic() >= w.giveaway_hold_until
                               and w.stream_id not in bought_streams]
                    if not disc_ws:
                        continue
                    victim = min(disc_ws, key=lambda w: w.opened_at)
                    log(f"{victim.seller}: evicting discovery tab for configured seller")
                    victim.close()
                    del watchers[victim.stream_id]
                open_watcher(sid, seller, url, "seller")
            if not discovery_on:
                return
            # Rotate long-held discovery slots when other streams are waiting.
            waiting = [s for s in discovered
                       if s not in watchers and s not in seller_live
                       and not blocked(s)]
            if waiting:
                due_now = []
                for w in list(watchers.values()):
                    age = time.monotonic() - w.opened_at
                    due = (age > ROTATE_SECONDS
                           or (age > IDLE_ROTATE_SECONDS and not w.saw_giveaway))
                    # A stream we've bought in keeps its tab for the whole show:
                    # we paid for buyers-giveaway eligibility, so we must be
                    # watching when those fire.
                    if (w.source == "discovery" and due
                            and time.monotonic() >= w.giveaway_hold_until
                            and w.stream_id not in bought_streams):
                        due_now.append((age, w))
                # Close at most as many tabs as there are streams waiting to
                # take their place. Without this, a single waiting stream in a
                # small pool churns EVERY idle tab every cycle — which is what
                # tripped Cloudflare (~4 page loads/min of pure shuffling).
                due_now.sort(key=lambda t: -t[0])  # longest-held first
                for _age, w in due_now[:len(waiting)]:
                    log(f"{w.seller}: rotating watch slot"
                        + (" (idle)" if not w.saw_giveaway else ""))
                    w.close()
                    del watchers[w.stream_id]
            # Fill free slots: least-recently-watched first, but sellers seen
            # running an enterable giveaway in the last hour jump the queue
            # (~7.5 min of credit) — informed sampling instead of blind LRU.
            # Never-watched streams keep top priority (base 0).
            def fill_priority(sid):
                # Streams we've bought in outrank everything: buyers-giveaway
                # eligibility was paid for and only lasts this show.
                if sid in bought_streams:
                    return -1e9
                base = last_watch_start.get(sid, 0.0)
                info = state["seller_giveaway_seen"].get(discovered[sid][0])
                if info:
                    try:
                        age = (datetime.now(timezone.utc)
                               - datetime.fromisoformat(info["at"])).total_seconds()
                        if age < 3600:
                            base -= 450
                    except (KeyError, ValueError):
                        pass
                return base

            for sid in sorted(waiting, key=fill_priority):
                if len(watchers) >= cap:
                    break
                if sid in watchers:
                    continue
                seller, url, title = discovered[sid]
                open_watcher(sid, seller, url, "discovery")

        def process_watchers():
            """Drain WS events from all watch tabs and notify. Called every
            main-loop tick AND between seller profile checks, so giveaway
            alerts aren't delayed by a long seller-poll cycle."""
            for sid, w in list(watchers.items()):
                # Detected my purchase here → this show's buyers-giveaways open up.
                if w.purchases:
                    w.purchases = []
                    if sid not in bought_streams:
                        log(f"{w.seller}: detected your purchase — buyers "
                            "giveaways enabled, tab pinned for this show")
                    bought_streams[sid] = time.monotonic()
                    state["bought_streams"][sid] = {
                        "seller": w.seller,
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                    save_state(state)
                eligible_here = (sid in bought_streams
                                 or w.seller.lower() in manual_bought)
                events = w.drain()
                for ev in events:
                    gid = ev["id"] or f"ws:{sid}:{int(time.time() // 600)}"
                    last_ws_giveaway[sid] = time.monotonic()
                    # Low-value prizes still earn rotation credit: a sticker
                    # streak signals an active giveaway host whose next round
                    # may be worth catching. Buyers-only doesn't (can't enter).
                    if not ev.get("buyers_only"):
                        state["seller_giveaway_seen"][w.seller] = {
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    # An eligible buyers-giveaway earns the hold (normally skipped
                    # for buyers-only) so the tab survives its draw.
                    if ev.get("buyers_only") and eligible_here:
                        w.giveaway_hold_until = max(
                            w.giveaway_hold_until, time.monotonic() + 360)
                    notify_giveaway(w.seller, w.stream_url, gid, ev["title"],
                                    buyers_only=ev.get("buyers_only", False),
                                    followers_only=ev.get("followers_only", False),
                                    ends_in=ev.get("ends_in"),
                                    low_value=ev.get("low_value", False),
                                    buyers_eligible=eligible_here,
                                    is_pack=ev.get("is_pack", False),
                                    detected_at=ev.get("detected_at"),
                                    unenterable_domestic=ev.get(
                                        "unenterable_domestic", False))
                wins, w.wins = w.wins, []
                for win in wins:
                    entries = win.get("entries", [])
                    log(f"{w.seller}: giveaway won by {win['username']}"
                        f" ({len(entries)} entries) — {win.get('title', '')[:50]}")
                    # Whatnot requires the qualifying purchase to come AFTER a
                    # BAG is created, so a purchase is good for one BAG only.
                    # Once that BAG is drawn, the eligibility is spent.
                    if win.get("buyers_only"):
                        spent = False
                        if sid in bought_streams:
                            bought_streams.pop(sid, None)
                            state["bought_streams"].pop(sid, None)
                            spent = True
                        if w.seller.lower() in manual_bought:
                            manual_bought.discard(w.seller.lower())
                            remove_bought_seller(w.seller)
                            spent = True
                        if spent:
                            save_state(state)
                            log(f"{w.seller}: buyers giveaway drawn — purchase "
                                "spent, buyer-eligibility cleared (buy again "
                                "during the next one to qualify)")
                    # Race outcome: did we get a seat, and did the pool cap?
                    race = alerted_races.pop(win["giveaway_id"], None)
                    if race:
                        # The entrant NAME list is truncated at 50 (measured:
                        # counter=105 with list=50), so presence proves entry
                        # but ABSENCE proves nothing. Report the authoritative
                        # counter as the real pool size, and only claim
                        # confirmation when we actually saw the name.
                        seen = my_username and my_username in (
                            e.lower() for e in entries)
                        peak = max(win.get("entry_peak", 0), len(entries))
                        lat = race["latency"]
                        truncated = len(entries) >= LIST_TRUNCATION
                        parts = [
                            f"{w.seller}: DRAW "
                            f"{'pack' if race['is_pack'] else 'other'}",
                            f"alert_latency="
                            f"{f'{lat:.1f}s' if lat is not None else '?'}",
                            f"entries={peak}"
                            + (f" (odds {100.0 / peak:.1f}%)" if peak else ""),
                            f"you={'confirmed' if seen else 'unconfirmed'}",
                        ]
                        if truncated and not seen:
                            parts.append("name list truncated at 50 — "
                                         "absence is not evidence")
                        log(" | ".join(parts))
                    if not my_username:
                        continue
                    if win["username"].lower() == my_username:
                        notify_win(w.seller, w.stream_url,
                                   win["giveaway_id"], win.get("title", ""))
                    elif my_username in (e.lower() for e in entries):
                        # You were still in the entrant list at draw time. Push
                        # disabled by preference (🎲 collided with 🎁/🏆 banners);
                        # the log line keeps the entry-counted receipt on disk.
                        log(f"{w.seller}: you were in the draw "
                            f"({len(entries)} entries), didn't win")
                # DOM fallback for giveaways the WS missed; suppressed when
                # the WS reported one recently (it's the same giveaway).
                # Rate-limited: the main tick is now ~2s for latency, but this
                # locator query doesn't need to run that often.
                if (not events and time.monotonic() - w.last_dom_check > 30):
                    w.last_dom_check = time.monotonic()
                    if w.dom_shows_giveaway() and (
                            time.monotonic() - last_ws_giveaway.get(sid, -1e9) > 600):
                        gid = f"dom:{sid}:{int(time.time() // 900)}"
                        notify_giveaway(w.seller, w.stream_url, gid, "")
                if w.ended:
                    log(f"{w.seller}: stream ended — closing tab")
                    recently_ended[sid] = time.monotonic()
                    bought_streams.pop(sid, None)  # per-show buyer flag ends
                    if state["bought_streams"].pop(sid, None):
                        save_state(state)
                    w.close()
                    del watchers[sid]

            # Flush deferred sends once the loud banner had its moment.
            # Giveaways first — they're time-critical; 🎲 results can wait.
            if (deferred_giveaways
                    and time.monotonic() - last_loud_send["t"] > LOUD_GAP_SECONDS):
                seller, url, gid, gtitle, followers_only, ends_at = \
                    deferred_giveaways.pop(0)
                ends_in = (ends_at - time.monotonic()) if ends_at is not None else None
                if ends_in is not None and ends_in < 5:
                    log(f"{seller}: deferred giveaway expired before sending "
                        f"— {gtitle or 'untitled'}")
                else:
                    notify_giveaway(seller, url, gid, gtitle,
                                    followers_only=followers_only, ends_in=ends_in)
            elif (deferred_results
                    and time.monotonic() - last_loud_send["t"] > LOUD_GAP_SECONDS):
                title, body, url, gid, seller = deferred_results.pop(0)
                if gid not in state["notified_wins"]:
                    try:
                        notifier.send(title, body, url,
                                      priority="default", group="results")
                        state["notified_wins"][gid] = {
                            "seller": seller,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                        save_state(state)
                    except Exception as e:
                        log(f"draw-participation notification failed "
                            f"({e.__class__.__name__}: {e})")
                        deferred_results.append((title, body, url, gid, seller))

        try:
            log("Loading whatnot.com...")
            try:
                poll_page.goto(BASE_URL, wait_until="domcontentloaded")
                poll_page.wait_for_timeout(3000)
            except Exception as e:
                log(f"initial page load failed ({e.__class__.__name__}: {e})")
            if check_session(poll_page):
                return
            last_login_check = time.monotonic()

            while not stop["requested"]:
                if poll_page.is_closed():
                    log("Browser window was closed externally — stopping.")
                    try:
                        notifier.send(
                            "⚠️ Radar stopped: browser closed ⚠️",
                            "Restart with: monitor.py run",
                            BASE_URL, priority="high",
                        )
                    except Exception:
                        pass
                    return

                if sellers and time.monotonic() >= next_seller_poll:
                    live_now = {}
                    for seller in sellers:
                        if stop["requested"]:
                            break
                        if is_blocked(seller):
                            continue
                        # Skip sellers we're backing off from (offline streak).
                        if time.monotonic() < seller_next_check.get(seller, 0):
                            continue
                        # The discovery snapshot already lists live streams with
                        # sellers — reuse it and skip the profile page load.
                        result = next(
                            ((url, sid, t) for sid, (s, url, t) in discovered.items()
                             if s == seller), None)
                        if result is not None:
                            stream_url, stream_id, title = result
                            live_now[stream_id] = (seller, stream_url, title)
                            if stream_id not in state["notified_streams"]:
                                log(f"{seller}: LIVE — {title[:60]}")
                                state["notified_streams"][stream_id] = {
                                    "seller": seller,
                                    "at": datetime.now(timezone.utc).isoformat(),
                                }
                                save_state(state)
                            continue
                        try:
                            result = check_seller_live(poll_page, seller)
                        except Exception as e:
                            log(f"{seller}: check failed ({e.__class__.__name__}: {e})")
                            continue
                        finally:
                            # Keep giveaway alerts flowing during long polls.
                            process_watchers()

                        if check_session(poll_page):
                            return
                        last_login_check = time.monotonic()

                        if result is None:
                            # Back off: 90s -> 3m -> 6m -> 10m while offline.
                            streak = seller_offline_streak.get(seller, 0) + 1
                            seller_offline_streak[seller] = streak
                            delay = min(seller_poll * (2 ** min(streak - 1, 3)),
                                        OFFLINE_BACKOFF_MAX)
                            seller_next_check[seller] = time.monotonic() + delay
                            log(f"{seller}: offline (next check ~{int(delay)}s)")
                            continue
                        seller_offline_streak.pop(seller, None)
                        seller_next_check.pop(seller, None)
                        stream_url, stream_id, title = result
                        live_now[stream_id] = (seller, stream_url, title)
                        if stream_id in state["notified_streams"]:
                            log(f"{seller}: live (already notified) — {title[:60]}")
                            continue
                        # Live notifications disabled by user preference —
                        # giveaway alerts are the signal that matters.
                        log(f"{seller}: LIVE — {title[:60]}")
                        state["notified_streams"][stream_id] = {
                            "seller": seller,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                        save_state(state)
                        snooze(jitter(3))
                    seller_live = live_now
                    next_seller_poll = time.monotonic() + jitter(seller_poll)

                if discovery_on and time.monotonic() >= next_disc_poll:
                    found = {}
                    any_failed = False
                    for source in disc_sources:
                        try:
                            for url, sid, seller, title in discover_streams(
                                    poll_page, source, disc_query):
                                if is_blocked(seller):
                                    continue
                                found[sid] = (seller, url, title)
                        except Exception as e:
                            any_failed = True
                            name = source.get("name", source.get("feedId", "?"))
                            log(f"discovery '{name}' failed "
                                f"({e.__class__.__name__}: {e})")
                    if any_failed and not found:
                        # Transient failure must not wipe the snapshot — that
                        # would close every discovery tab as "no longer listed".
                        log("discovery: poll failed — keeping previous snapshot")
                    else:
                        discovered = found
                    log(f"discovery: {len(discovered)} live streams, "
                        f"{len(watchers)} tabs open")
                    # Expire manual buyer-eligibility when the seller's show ends:
                    # eligibility is per-show, so once a seller we were armed for
                    # is no longer live, clear it (and unset it in config so a
                    # restart won't re-arm a stale flag).
                    if manual_bought:
                        live_sellers = {s.lower() for s, _, _ in discovered.values()}
                        live_sellers |= {w.seller.lower() for w in watchers.values()}
                        for seller in list(manual_bought):
                            if seller in live_sellers:
                                manual_seen_live.add(seller)
                            elif seller in manual_seen_live:
                                manual_bought.discard(seller)
                                manual_seen_live.discard(seller)
                                remove_bought_seller(seller)
                                log(f"buyer-eligibility for {seller} cleared "
                                    "— show ended")
                    horizon = time.monotonic() - 86400
                    for d in (recently_ended, last_watch_start, last_ws_giveaway,
                              bought_streams):
                        for k in [k for k, ts in d.items() if ts < horizon]:
                            del d[k]
                    # Periodic prune so multi-day runs don't grow state.json
                    # unbounded (prune_state otherwise only runs at startup).
                    prune_state(state)
                    if disc_notify_new:
                        for sid, (seller, url, title) in discovered.items():
                            if (sid in state["seen_streams"]
                                    or sid in state["notified_streams"]):
                                continue
                            try:
                                notifier.send(
                                    f"🔎 {seller} is live 🔎",
                                    title or "Discovered stream.",
                                    url, priority="default",
                                )
                            except Exception as e:
                                log(f"discovery notification failed "
                                    f"({e.__class__.__name__}: {e})")
                                continue
                            state["seen_streams"][sid] = {
                                "seller": seller,
                                "at": datetime.now(timezone.utc).isoformat(),
                            }
                            save_state(state)
                    next_disc_poll = time.monotonic() + jitter(disc_poll)

                manage_watchers()
                process_watchers()

                # Login heartbeat: profile checks normally verify the session,
                # but live pinned sellers resolve via the discovery snapshot
                # (no page load) — an expired session would silently turn the
                # watch tabs into anonymous viewers and stop presence-holding.
                if time.monotonic() - last_login_check > 3600:
                    try:
                        poll_page.goto(BASE_URL, wait_until="domcontentloaded")
                        poll_page.wait_for_timeout(3000)
                        if check_session(poll_page):
                            return
                    except Exception as e:
                        log(f"login heartbeat failed ({e.__class__.__name__}: {e})")
                    last_login_check = time.monotonic()

                # Giveaways run ~5 minutes, so alert latency eats into the
                # window you have to enter. Draining open tabs is purely local
                # (no Whatnot traffic), so tick fast whenever tabs are open.
                snooze(jitter(FAST_TICK_SECONDS) if watchers
                       else jitter(min(giveaway_poll, 10)))
        except KeyboardInterrupt:
            pass
        finally:
            log("Shutting down.")
            for w in watchers.values():
                w.close()
            save_state(state)
            ctx.close()
            log("Bye.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Whatnot giveaway radar: detect giveaways, notify my phone. "
        "Entry is always manual.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="open a headful browser to log in to Whatnot")
    sub.add_parser("test", help="send a test notification to your phone")
    sub.add_parser("run", help="watch sellers/streams and notify (long-running)")
    args = parser.parse_args()

    config = load_config()
    {"login": cmd_login, "test": cmd_test, "run": cmd_run}[args.command](config)


if __name__ == "__main__":
    main()

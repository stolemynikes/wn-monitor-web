# Whatnot Giveaway Radar

Watches Whatnot livestreams and pushes a notification to your phone the moment a
giveaway starts, with a link straight into the stream.

**It detects and notifies. It never enters anything for you.** Whatnot requires
giveaway entries to be made manually — you tap the notification and enter in the
app yourself. Anything else is against their rules and will get entries voided
and accounts limited.

---

## What it does

- Finds live streams in a category/country via Whatnot's own browse feed.
- Keeps a few muted tabs open and watches each stream's realtime channel.
- Notifies you when a giveaway starts, and again if you win.
- Filters out what you can't or don't want to enter: buyers-only giveaways,
  low-value prizes, and giveaways restricted to another country.
- Holds a stream's tab open through the draw, so an entry you made stays
  counted even after you switch away on your phone.

## Requirements

- Python 3.11+
- A phone notification app — [Bark](https://apps.apple.com/app/id1403753865)
  (iOS, recommended) or [ntfy](https://ntfy.sh) (iOS/Android)
- A Whatnot account you log into once, in the tool's own browser
- ~1 GB free disk (Chromium download plus its cache)

## Setup

```bash
git clone https://github.com/stolemynikes/wn-monitor-web
cd wn-monitor-web

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp config.example.json config.json
python web.py            # open http://127.0.0.1:8765
```

Then, in the panel:

1. **Notifications** — pick Bark or ntfy, paste your key/topic, hit *Send test*
   and confirm your phone buzzes.
2. **Browser profile → Login** — a browser opens; log in to Whatnot, then click
   *I'm done*. This session is stored only on your machine.
3. **Discovery** — choose a category and one or more shipping countries.
4. **Start**.

Blocked sellers, tab count and poll intervals are all editable in the panel.
Config changes need a restart, which the panel tells you about.

## Reaching it from your phone

The panel binds to `127.0.0.1` — only your machine. To use it from your phone,
put both devices on a private network such as [Tailscale](https://tailscale.com)
and run `python web.py --host 0.0.0.0`. **Do not expose it to the open
internet**: it can launch a browser and read your configuration.

## Command line

The panel is optional; everything works headless:

```bash
python control.py start|stop|restart|status
python monitor.py test        # send a test notification
python monitor.py run         # run in the foreground
```

## Please be gentle

Every tab and page load is traffic to someone else's service, and the defaults
here are deliberately conservative: 3 tabs, 5-minute idle rotation, 90-second
polling.

If you crank those up, two things happen — Whatnot's bot protection notices, and
the tool stops working. It already happened once during development: too many
tabs cycling too fast triggered a Cloudflare challenge that locked the browser
out for hours.

**If you see `Bot challenge encountered`:**

- Stop. Don't restart in a loop — each retry against an active challenge makes
  it worse and prolongs it.
- Leave it off for a few hours; these flags decay on their own.
- Come back with fewer tabs and longer intervals.
- Don't try to disguise the browser. Fingerprint spoofing is what turns a
  harmless, temporary edge block into an account problem.

Also worth knowing: giveaways run about five minutes, and entry lists in busy
streams are shown truncated at 50 names — so absence from a list doesn't prove
you weren't entered.

## Privacy

Everything is local. Your config, your Whatnot session and your logs never leave
your machine; the only outbound calls are to Whatnot and to your own
notification service. `config.json`, `state.json`, `radar.log` and the browser
profile are all gitignored — don't commit them, they contain your session and
your notification key.

## Adding a category

The panel ships the Pokémon-cards feed. Other categories need their feed id,
which you can capture yourself: open the browse page for that category with
devtools open, apply the *Shipped from* filter, find the outgoing `GetFeed`
GraphQL request, and copy `variables.feedId` into `config.json` under
`discovery.sources`.

## Not supported, on purpose

Automating giveaway entry, and disguising the browser's fingerprint. Both break
Whatnot's rules, and the second is the one most likely to cost you your account.

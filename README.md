# Whatnot Giveaway Radar

**It watches Whatnot streams for you and buzzes your phone the second a giveaway
starts.** Tap the notification, you're in the stream, you enter. That's it.

Think of it as a friend who sits at your computer watching six Whatnot streams
all evening and shouts "giveaway!" the moment one starts. It never gets bored
and never blinks.

### The one rule that matters

**It does NOT enter giveaways for you. You always tap and enter yourself.**

Whatnot's rules say entries must be made by a real person, by hand. This tool
only *watches and tells you*. Anything that enters automatically is against
their rules, gets your entries thrown out, and can get your account limited.
That's not a limitation we forgot to fix — it's on purpose.

---

## What you need before you start

| What | Why | Cost |
|---|---|---|
| A computer that stays on | It does the watching. Closing the lid stops it. | — |
| A Whatnot account | It watches while logged in as you | free |
| **Bark** app (iPhone) or **ntfy** (Android) | This is what buzzes your phone | free |
| About 1 GB of free space | It downloads its own mini web browser | — |
| ~20 minutes, once | Setup | — |

You do **not** need to know how to code. You'll copy and paste a few lines, then
everything else happens in a normal window with buttons.

---

## Part 1 — Install it (once)

1. **Get the files.** On the GitHub page click the green **Code** button →
   **Download ZIP**, then unzip it. (Or `git clone` if you know what that is.)

2. **Check you have Python.** The tool needs Python 3.11 or newer. If you're not
   sure, skip ahead — the next step will tell you if it's missing and where to
   get it.

3. **Double-click the launcher** in the unzipped folder:

   - **Mac** → `start-panel.command`
   - **Windows** → `start-panel.bat`

That's it. The first run installs everything it needs — a few minutes, once —
then your browser opens at **http://127.0.0.1:8765** showing the panel. Every
time after that, the same double-click just opens the panel in a couple of
seconds.

**On a Mac**, the first double-click may be refused because the file came from
the internet. Right-click it → **Open** → **Open**. You only do this once.

**If it says Python is missing**, install it from
[python.org/downloads](https://www.python.org/downloads/) — on Windows tick
*"Add Python to PATH"* — then double-click the launcher again.

Leave the black window open while you use the panel; closing it shuts the panel
down.

## Part 2 — Set it up in the panel (once)

Everything from here is clicking buttons.

### 1. Make your phone buzz

First install **Bark** (iPhone) or **ntfy** (Android) on your phone.

- **Bark**: open it. You'll see a web address with a code in it, like
  `https://api.day.app/AbCdEf123456/`. The middle part — `AbCdEf123456` — is
  your key. Copy it.
- **ntfy**: open it, tap **+** to subscribe to a new topic, and invent a long
  weird name like `giveaways-x7k2m9qp4z`. Anyone who guesses your topic name can
  read your notifications, so make it long and random. That name is your topic.

In the panel, under **Notifications**: pick Bark or ntfy, paste your key or
topic in the box, click **Save**, then click **Send test**.

**Your phone should buzz.** If it doesn't, see Troubleshooting below. Don't
continue until this works — the whole tool is useless without it.

### 2. Log in to Whatnot

Under **Browser profile**, click **Login**.

A browser window opens by itself. Log in to Whatnot in it, exactly like normal.
Then come back to the panel and click **I'm done**. The window closes.

That window is the tool's own private browser — separate from your normal one.
Your usual browser, bookmarks and logins are never touched.

### 3. Choose what to watch

Under **Discovery**, pick a category and one or more countries (hold `Cmd` or
`Ctrl` to pick several), then click **Apply**.

Countries mean *where the seller ships from*. Pick the ones you can actually
receive parcels from. Sellers often restrict giveaways to their own country, and
the tool automatically hides those you can't enter.

### 4. Press Start

Click **Start** at the top. Within a minute you'll see lines appearing in the
**Live log** box — that's it finding streams and opening tabs.

Now leave it alone and wait for your phone to buzz.

---

## Part 3 — Using it day to day

- **Start / Stop** — the two buttons at the top. Stop it when you don't want it.
- **Blocked sellers** — annoying seller? Type their username and click **Block**.
  They disappear completely: no watching, no notifications.
- **Live log** — what it's doing right now. Useful when something seems wrong.
- **Tuning** — how many streams to watch at once. **Leave this alone unless you
  have a reason.** See "Going easy" below.

A browser window sits open on your computer while it runs. That's the tool
doing its job — **don't close it**, that stops the radar.

The radar never moves, resizes or minimises that window. A window in an odd
position or size is something web pages can read, and on Windows a minimised
window reports itself 32,000 pixels off-screen — which in testing drew
"checking your browser" pages that didn't otherwise appear.

To get it out of the way, put it on a second desktop (`Win+Ctrl+D`, then
`Win+Tab` and drag it across; Mission Control on a Mac) or just leave it behind
your other windows. Neither changes anything the page can see. New tabs open in
the background, so it won't jump in front of you while you're working.

It checks itself rather than trusting that this is safe. On start it minimises,
watches for a few seconds to confirm Whatnot still sees the page as visible and
its timers running at full speed, and puts the window back on screen if not.
The log line tells you which happened. So the worst case is a window you can
see, never a radar that quietly stopped holding your giveaway entries.
Minimising it by hand is fine too.

### What the notifications mean

| You see | It means | Do |
|---|---|---|
| 🎁 **Giveaway — sellername** | A giveaway just started | Tap it, enter in the app |
| 🛒🔔 **Buyers giveaway** | A giveaway only for people who bought something | Only enterable if you buy in that stream |
| 🏆 **You WON!** | You won something | Check the Whatnot app |
| ⚠️ **Radar stopped** | Something needs you | See Troubleshooting |

**A useful trick:** once you've tapped a giveaway and entered, you can close the
Whatnot app. As long as the radar is still watching that stream on your
computer, your entry stays counted through the draw.

---

## Going easy (please read this bit)

Every stream it watches is real traffic to Whatnot's servers. The settings it
comes with are deliberately gentle: **3 streams at a time**, checking slowly.

If you turn those up to be greedy, two things happen: Whatnot's security notices
the unusual activity, and **the tool stops working** — for hours.

This isn't hypothetical. During development, too many streams cycling too fast
set off Whatnot's bot protection and locked the browser out for most of an
evening. Watching a few streams politely works far better than watching
everything and getting blocked.

**If you see `Bot challenge encountered` in the log:**

1. **Stop. Don't keep pressing Start.** Every retry while you're blocked makes it
   last longer.
2. Leave it off for a few hours. These blocks fade by themselves.
3. Start again later with *fewer* streams.
4. **Don't install anything that "hides" or "fakes" the browser** to get around
   it. That turns a harmless temporary block into something that can affect your
   actual Whatnot account.

---

## Troubleshooting

**My phone didn't buzz on the test.**
Check the app is installed and you allowed notifications when it asked. Check
you pasted the key correctly (no spaces). On iPhone, if the notification appears
but is silent, look in Settings → Notifications → Bark and turn sounds on.

**It says "config.json missing".**
The launcher normally creates this for you. Double-click `start-panel.command`
(Mac) or `start-panel.bat` (Windows) — running it again is safe and will make
the file.

**The panel page won't load.**
The panel only runs while its window is open. Double-click `start-panel.command`
(Mac) or `start-panel.bat` (Windows) to start it again.

**It says `No module named 'fastapi'` (or similar).**
You used the wrong Python. Use the full path — `.venv/bin/python web.py` — or
just double-click the launcher, which always gets it right.

**It says "refusing to start: another radar is running".**
You already have one running somewhere. Two at once doubles the traffic and gets
you blocked. Stop the other one.

**"Not logged in" even though I logged in.**
Click Login again and make sure you press **I'm done** in the panel afterwards.

**It says the profile is over a gigabyte.**
Normal — the browser saves copies of everything it loads. Click **Clear cache**.
That's safe and keeps you logged in. (**Clear site data** is the red one — that
logs you out and you'd have to log in again.)

**I stopped getting notifications.**
Look at the log. If there's a bot challenge, read the section above. If it's
just quiet, there may genuinely be no giveaways — it only watches a few streams
at a time, so it won't catch every giveaway that happens.

---

## Your privacy

Everything stays on your computer. Your Whatnot login, your settings and your
logs never get sent anywhere. The only things it talks to are Whatnot itself and
your own notification app.

The panel is only reachable from your own machine unless you deliberately open
it up — see below.

If you ever share this folder, don't include `config.json` or the
`whatnot-profile` folder: they contain your login and your notification key.
(They're already excluded from git for exactly this reason.)

## Using the panel on your phone

Open the panel on your computer and look at the **"use on your phone"** card —
it walks you through it and shows a QR code you can point your camera at, so you
never have to type an address. It knows your computer's name, so the
instructions there are already filled in for you.

The short version:

1. Install [Tailscale](https://tailscale.com) on the computer and the phone, and
   sign in to both with the **same account**.
2. In the panel's "use on your phone" card, click **allow my phone**.
3. Close the black window and double-click the launcher again. (That's the only
   way the change takes effect — the panel reads the setting when it starts.)
4. Scan the QR code. Your phone asks once for the password shown on that card,
   and remembers it.

Step 3 matters: until you restart it, the panel only answers to the computer
it's running on, so your phone will scan the code and get nothing.

**Don't put the panel on the open internet.** Anyone who found it could start
your radar, read your log and change your settings. Tailscale keeps it reachable
only from your own devices, which is what you actually want.

---

## Some honest expectations

- It watches **a few** streams at a time, not all of them. You'll miss
  giveaways. That's the trade for not getting blocked.
- Popular giveaways have **50 or more people** entering. Winning is luck; this
  just gets you into more draws than you'd manage by hand.
- It can break. It relies on how Whatnot's website works today, and if Whatnot
  changes things, parts will stop working until someone updates the code.

---

## Running it on a server

Don't want to leave your computer on? See **[docs/server.md](docs/server.md)**
for Linux/VPS setup — but read the first section: a VPS is the *worst* of the
options because datacenter IP addresses are treated as suspicious by Whatnot's
bot protection. An old laptop or a Raspberry Pi at home works much better.

## For technical users

- `python control.py start|stop|restart|status` — run it without the panel.
- `python monitor.py run` — foreground, logs to stdout.
- `python monitor.py test` — send a test notification.
- The panel opens in its own Chrome app window so it can't end up as a tab
  among the radar's streams. `--tab` for an ordinary tab, `--no-browser` for
  none.
- Config lives in `config.json`; the panel only writes an allowlist of fields.
- The panel binds `config.panel_host` (default `127.0.0.1`) on port 8765. The
  **allow my phone** button writes that field; `--host` overrides it for one
  run. Either way, read the warning above.
- Discovery uses Whatnot's own `GetFeed` GraphQL call. Only the Pokémon feed id
  is bundled. To add a category: open its browse page with devtools, apply the
  *Shipped from* filter, find the `GetFeed` request, and copy
  `variables.feedId` into `discovery.sources` in `config.json`.
- Deliberately unsupported: automating entry, and browser-fingerprint spoofing.

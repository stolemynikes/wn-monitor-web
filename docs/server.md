# Running on a Linux server (VPS, Raspberry Pi, old laptop)

Keeps the radar going without leaving your desktop machine awake.

---

## Read this before buying a VPS

**A VPS is the worst choice of the three, and it's worth understanding why
before you spend money on one.**

Cloudflare — which protects Whatnot — scores traffic partly on where it comes
from. Datacenter IP ranges (Hetzner, DigitalOcean, AWS, Vultr, OVH…) are where
most of the internet's bot traffic originates, so they start with a poor
reputation before you send a single request. Home broadband addresses do not.

This tool already runs a browser that is *detectably automated*. On a
residential connection that's usually tolerated. From a datacenter address it
stacks a second strong bot signal on top of the first, and you're considerably
more likely to be met with an endless "checking your browser" loop than you are
at home.

So, in order of what actually works:

| Where | IP reputation | Notes |
|---|---|---|
| **Old laptop at home** | residential (best) | Free. Easiest. Just stop it sleeping. |
| **Raspberry Pi 5 / mini PC at home** | residential (best) | ~5W, silent, ~€90–150. |
| **VPS** | datacenter (worst) | Always-on and convenient, but expect challenges. |

The usual "fix" for datacenter reputation is a residential proxy. Don't — that's
deliberately disguising where your traffic comes from, it breaks Whatnot's rules,
and it escalates a browser-level block into an account-level one.

If you're testing a VPS anyway, everything below works. Just know what you're
trading.

---

## Requirements

- Debian/Ubuntu (other distros fine, adjust package names)
- **2 vCPU, 4 GB RAM** for 3 streams. Each Chromium tab is ~400–600 MB.
  6 streams wants 8 GB.
- ~2 GB disk for Chromium and its cache

## Install

```bash
sudo apt update
sudo apt install -y python3-venv git xvfb

git clone https://github.com/stolemynikes/wn-monitor-web
cd wn-monitor-web
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium

cp config.example.json config.json
```

`xvfb` is not optional. A server has no screen, but **true headless Chromium is
blocked by Whatnot's bot protection** — so the real browser has to run against a
virtual display. `control.py` detects a Linux box with no `DISPLAY` and wraps the
monitor in `xvfb-run` automatically; it will tell you if `xvfb` is missing.

## Logging in to Whatnot

The login is interactive and there's no screen, so pick one:

**A. Copy the profile from a machine where you've already logged in** (simplest)

```bash
# on your desktop, with the radar stopped:
tar czf profile.tgz whatnot-profile
scp profile.tgz user@server:~/wn-monitor-web/
# on the server:
cd ~/wn-monitor-web && tar xzf profile.tgz && rm profile.tgz
```

Your session is portable, but Whatnot may notice the sudden change of IP and ask
you to verify. Clear the cache first (`Clear cache` in the panel) so you're
copying ~15 MB instead of a gigabyte.

**B. Log in over a forwarded display**

```bash
ssh -X user@server                       # needs X11 on your desktop
cd wn-monitor-web && .venv/bin/python monitor.py login
```

Slow but workable. VNC to a desktop session also works if the server has one.

## Run it as a service

```bash
sudo tee /etc/systemd/system/wn-radar.service >/dev/null <<EOF
[Unit]
Description=Whatnot radar control panel
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/wn-monitor-web
ExecStart=$HOME/wn-monitor-web/.venv/bin/python web.py --host 0.0.0.0
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wn-radar
```

That runs the **panel**, which survives reboots; start and stop the radar itself
from the panel. To see the generated password:

```bash
journalctl -u wn-radar | grep -i password
```

## Reaching the panel

Install [Tailscale](https://tailscale.com) on the server and your phone, then
open the panel's **"use on your phone"** card — it shows the address, a QR code
and the password.

**Do not open port 8765 in your firewall or point a public domain at it.** The
panel can start a browser, read your logs and change your settings; anything
publicly reachable will be found by scanners within hours. Tailscale keeps it
private with no ports open at all.

## Troubleshooting

**`no display and xvfb-run not found`** — `sudo apt install xvfb`.

**Chromium fails to launch** — you probably skipped `--with-deps`. Run
`.venv/bin/playwright install-deps chromium`.

**Endless "checking your browser"** — the datacenter-IP problem described at the
top. Stop, wait, and try again later with fewer tabs. Don't retry in a loop, and
don't reach for proxies or stealth plugins.

**Killed / out of memory** — Chromium was OOM-killed. Reduce
`max_concurrent_streams`, or add RAM/swap.

**Everything looks fine but no notifications** — check the radar is actually
running (not just the panel), and that the session is still logged in; both are
shown at the top of the panel.

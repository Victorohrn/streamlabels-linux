# streamlabels-linux

A Linux replacement for the Streamlabs **Stream Labels** app. It keeps a folder of small
text files (`most_recent_follower.txt`, `session_top_donator.txt`, `all_time_top_cheerer.txt`, …)
up to date while you stream, so OBS text sources can show your latest follower, top tipper,
sub count and so on, exactly like the Windows/macOS app did.

Streamlabs never shipped the Stream Labels app or their OBS plugin for Linux. This daemon talks
to the same Streamlabs socket that Streamlabs Desktop uses, and optionally to Twitch directly.

* Streamlabs **alerts** (Alert Box, Event List, Goals, Chat Box) are *browser sources* and already
  work in OBS on Linux. Copy the widget URL from the Streamlabs dashboard into an OBS Browser
  Source. Nothing in this repo is needed for alerts.
* Stream **labels** (text files) are what was missing. That is what this provides.

## What you get

A `StreamLabels` folder (default `~/Documents/StreamLabels`, localised, e.g. `~/Dokument/StreamLabels`)
with one `.txt` per label. File names follow the Streamlabs Desktop names, so scenes built on
Windows keep working.

| Followers | Subscribers | Sub gifters |
|---|---|---|
| most_recent_follower | most_recent_subscriber | most_recent_sub_gifter |
| session_most_recent_follower | session_most_recent_subscriber | session_most_recent_sub_gifter |
| session_followers (list) | session_subscribers (list) | session_sub_gifters (list) |
| session_follower_count | session_subscriber_count | session_sub_gifter_count |
| total_follower_count * | total_subscriber_count * | session_gifted_sub_count |
| | | session_top_sub_gifter, all_time_top_sub_gifter |

| Tips (Streamlabs donations) | Bits / cheers | Raids |
|---|---|---|
| most_recent_donator, most_recent_donation_amount | most_recent_cheerer, most_recent_cheer_amount | most_recent_raider, most_recent_raid_viewers |
| session_most_recent_donator | session_most_recent_cheerer | session_most_recent_raider |
| session_donators (list), session_donator_count | session_cheerers (list), session_cheerer_count | session_raiders (list), session_raider_count |
| session_donation_amount | session_bits_amount | session_top_raider |
| session_top_donator, session_top_donation | session_top_cheerer, session_top_cheer | |
| session_biggest_donation | | |
| all_time_top_donator, all_time_top_donation | all_time_top_cheerer, all_time_top_cheer | |
| 30day_top_donator, 30day_top_donation | 30day_top_cheerer | |
| total_donation_amount | | |

`*` needs the optional Twitch connection (see below). "Top" means the highest *cumulative* amount
per person in that window. "All time" and "30 day" count from when you first ran the daemon,
unless Streamlabs sends its own ready-made values (see *How it works*).

Any label can be reformatted in the config, e.g. `most_recent_donator = "{name} - {formatted}"`.
Run `streamlabels test` to fill every file with sample values while you lay out your scene.

## How it works

Streamlabs Desktop is open source ([streamlabs/desktop](https://github.com/streamlabs/desktop),
GPL-3.0). Its `app/services/streamlabels/index.ts` shows that labels arrive over the Streamlabs
Socket.IO server (`https://sockets.streamlabs.com`) as ordinary socket events: individual
`follow` / `subscription` / `donation` / `bits` / `raid` events, plus a `streamlabels` event that
carries a complete `{filename: value}` map computed server-side.

This daemon:

1. connects to that socket with your account's **Socket API token** (the public one from the
   dashboard, same one third-party bots use);
2. turns every event into label updates and persists them (`~/.local/state/streamlabels/state.json`),
   so "most recent follower" survives restarts;
3. if Streamlabs pushes a `streamlabels` map, writes those values too and lets them win over
   its own computed values for the same file name (they have your full account history);
4. optionally connects straight to Twitch (EventSub websocket + Helix) for follower/sub totals
   and for events even when Streamlabs hiccups.

Files are written atomically; OBS re-reads a file about once a second.

## Install

Requires Python 3.11+ (CachyOS/Arch: `sudo pacman -S python`), `git`, and OBS with a text source.

```bash
git clone https://github.com/victorohrn/streamlabels-linux ~/streamlabels-linux   # or copy this folder anywhere
~/streamlabels-linux/install.sh
```

The installer creates a private virtualenv in `~/.local/share/streamlabels`, links
`~/.local/bin/streamlabels`, writes `~/.config/streamlabels/config.toml`, and registers a
`systemd --user` service (not started yet). Re-run it to upgrade. `uninstall.sh` reverses it.

## Configure

1. Streamlabs dashboard → **Settings → API Settings → API Tokens → "Your Socket API Token"** → Copy.
2. Edit `~/.config/streamlabels/config.toml` and paste it into `socket_token = "..."`.
3. Start it:

```bash
systemctl --user enable --now streamlabels
journalctl --user -u streamlabels -f      # should say "Streamlabs socket connected"
```

Send a test alert from the Streamlabs dashboard: the journal logs it as an ignored TEST event
(set `count_test_events = true` to count them while you are setting things up).

Treat the socket token like a password. It only grants read access to your event stream, but
anyone who has it can see your tips and followers in real time. Regenerate it in the dashboard
if it leaks.

## OBS setup

For each label:

1. **Sources → + → Text (FreeType 2)**, name it after the label.
2. Tick **Read from file**, browse to `~/Documents/StreamLabels/<label>.txt`.
3. Pick font, colour, outline; position it.

Notes:

* Flatpak OBS (`com.obsproject.Studio`) ships `text-freetype2` and `obs-browser` and has host
  filesystem access, so reading the files works without extra permissions.
* Session lists (`session_followers`) are one line; set `list_separator = "\n"` for one name per
  line and tick *Chat log mode* or enable *Word wrap* in the text source if you prefer.
* The daemon only writes a file when its value changes, so OBS does not flicker.

Alerts and notifications: Streamlabs dashboard → **Alert Box** → copy the widget URL → OBS
**Browser Source**, 1920×1080, "Shutdown source when not visible" off. Same for Event List, Goals,
Chat Box. These are plain web pages; they work identically on Linux.

## Commands

```
streamlabels run [--dump] [-v]   run in the foreground (what the service does); --dump logs raw events
streamlabels init                write the config template and create folders
streamlabels test                fill every label file with sample values (for scene layout)
streamlabels show                print the current value of every label
streamlabels reset-session       start a new "session" (session_* labels back to zero)
streamlabels twitch-login        device-code login for the optional Twitch connection
streamlabels paths               print config/state/output/pid paths
```

`systemctl --user reload streamlabels` also resets the session (it sends SIGUSR1). By default a
new session starts whenever the daemon starts; set `session_reset_on_start = false` to reset only
manually, e.g. from a Stream Deck/keybind running `streamlabels reset-session`.

## Optional: talk to Twitch directly

Streamlabs relays Twitch follows, subs, bits and raids, so most people never need this. Enable it
to get `total_follower_count` / `total_subscriber_count`, to seed `most_recent_follower` from
Twitch on startup, and to keep receiving events if the Streamlabs socket is down.

1. <https://dev.twitch.tv/console/apps> → *Register Your Application* (needs 2FA on the Twitch
   account). Name: anything; OAuth redirect: `http://localhost`; Category: *Broadcaster Suite*;
   Client Type: **Public**. Copy the **Client ID**.
2. In the config: `[twitch] enabled = true`, `client_id = "..."`.
3. `streamlabels twitch-login` → open the printed URL *as the streamer*, enter the code.
4. `systemctl --user restart streamlabels`.

Scopes requested: `moderator:read:followers`, `channel:read:subscriptions`, `bits:read`. The token is
stored in `~/.config/streamlabels/twitch-token.json` (mode 600) and refreshed automatically.
When Twitch is enabled, the Twitch events relayed by Streamlabs are ignored so nothing is counted
twice (`relay_twitch_events = "auto"`).

## Troubleshooting

* `Streamlabs rejected the socket token`: re-copy the token; Streamlabs occasionally rotates
  them when you change account links.
* Files never change: `journalctl --user -u streamlabels -f`, then send a dashboard test alert
  with `count_test_events = true`. Also check `streamlabels paths` matches the folder OBS reads.
* Values look stale after a crash: the state file is only written after each event; nothing is
  lost except events that arrived while the daemon was down. With Twitch enabled, totals are
  re-polled every `poll_interval` seconds.
* Run `streamlabels run --dump -v` in a terminal to see every raw payload Streamlabs sends.
  If you see a `streamlabels` event, Streamlabs is giving you its ready-made label set and those
  file names (dozens of them) appear in the folder automatically.

## Why not port the official plugin?

The "Streamlabs Plugin for OBS" source is public ([stream-labs/obs-sl-browser](https://github.com/stream-labs/obs-sl-browser),
GPL-2.0) but it is a Windows-only Chromium Embedded Framework host: a separate `sl-browser.exe`
that embeds the Streamlabs web dashboard inside OBS and talks to the plugin over gRPC. The CMake
project declares a `WIN32` executable, its CI is PowerShell, and the label feature is just the
dashboard web app pushing text into OBS sources through that bridge. A Linux port would mean
re-doing the CEF host, gRPC bridge and Qt docking for Linux/Wayland to end up with a browser
window, which is not where the value is. The labels themselves come from the socket described
above, so a 600-line daemon reproduces the feature without any of that.

Other things that were checked and rejected: Streamlabs Desktop (no Linux build), the Stream
Labels app under Wine (Electron + login popups, fragile), OWN3D Pro plugin (archived Jan 2024,
X11 only), Casterlabs Caffeinated (Linux build not published), StreamElements overlays (browser
source labels work on Linux but tie you to StreamElements instead of Streamlabs tips).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # unit tests + an end-to-end test against a local fake Streamlabs socket
```

MIT licensed. Not affiliated with Streamlabs or Logitech.

# OpenHop ↔ Telegram Relay

Bridges an [OpenHop](https://github.com/openhop-dev) / MeshCore node and a Telegram
chat. Messages posted on a mesh channel (default: **General**) are relayed into a
Telegram chat, and replies in that Telegram chat are sent back out onto the same
mesh channel.

> **Heads up:** this was vibecoded — written quickly with an AI assistant. The
> API calls were checked against the installed `meshcore` library and the config
> and message-formatting logic were smoke-tested, but it has **not** been run
> against real hardware or a live Telegram chat, and there are no automated
> tests. Treat it as a starting point and verify it in your own setup.

It connects to the node as a **MeshCore companion client over TCP** — the same
protocol the phone/desktop companion apps use — so it can both read channel
content and originate outgoing channel messages. (The OpenHop repeater's HTTP
dashboard does not expose decrypted channel content, so the TCP companion path is
used instead.)

```
  mesh "General" channel  ──►  Telegram chat      (mesh_to_tg)
  Telegram chat reply     ──►  mesh "General"      (tg_to_mesh)
```

## Requirements

- Python 3.10+
- An OpenHop/MeshCore node reachable over TCP with the **companion server**
  enabled, and joined to the channel you want to relay.
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and the target
  chat ID.

## Setup

```bash
cd openhop-telegram-relay
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# then edit .env with your host/port, channel, bot token and chat id
```

## Configuration

All configuration is via environment variables (or a `.env` file). See
[.env.example](.env.example) for the full list. Key ones:

| Variable | Meaning | Default |
| --- | --- | --- |
| `OPENHOP_HOST` / `OPENHOP_PORT` | MeshCore TCP companion address | `127.0.0.1` / `4000` |
| `MESH_CHANNEL_NAME` | Channel to relay, resolved by name | `General` |
| `MESH_CHANNEL_INDEX` | Fallback index if the name can't be resolved | `0` |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | — |
| `TELEGRAM_CHAT_ID` | Target chat/group/channel id | — |
| `RELAY_DIRECTION` | `both`, `mesh_to_tg`, or `tg_to_mesh` | `both` |
| `MESH_MAX_CHARS` | Truncate outgoing mesh messages (LoRa is small) | `140` |
| `NOTIFY_NEW_NODES` | Alert when a node is seen for the first time | `true` |
| `NOTIFY_NODE_TYPES` | Which node types to alert on | `all` |
| `SEEN_NODES_FILE` | Where already-announced nodes are remembered | `seen_nodes.json` |
| `NODE_POLL_INTERVAL` | Contact-list diff interval for new-node alerts; `0` off | `300` |
| `WARDRIVING_ENABLED` | Watch a channel for wardrivers | `true` |
| `WARDRIVING_CHANNEL` | Channel to watch | `wardriving` |
| `WARDRIVING_QUIET_SECONDS` | Only alert after this much silence from them | `3600` |
| `TIMESYNC_HOST` / `TIMESYNC_PORT` | Endpoint the maintenance scripts use | falls back to `OPENHOP_*` |
| `RECONNECT_MIN_DELAY` / `RECONNECT_MAX_DELAY` | Reconnect backoff bounds (seconds) | `5` / `300` |
| `HEALTHCHECK_INTERVAL` | Liveness probe + keepalive interval; `0` disables | `45` |
| `NOTIFY_CONNECTION_EVENTS` | Tell the chat when the link drops/returns | `true` |

### Getting the chat ID

Create the group, add your bot to it, send `/start@YourBotName` in the group,
then run:

```bash
python3 scripts/get_chat_id.py
```

It reads the token from `.env` (or `TELEGRAM_BOT_TOKEN`, or a command-line
argument) and prints every chat the bot can see, with ids and types. Put the id
you want in `TELEGRAM_CHAT_ID`.

Three things that trip people up:

- **Send a command, not a greeting.** Bots have privacy mode on by default, so
  they only receive commands, @mentions and replies — plain group chatter is
  invisible and `getUpdates` will look empty. `/start@YourBotName` always
  arrives. (The relay ignores messages starting with `/`, so it won't be
  forwarded to the mesh.)
- **Use a group, not a broadcast channel.** Channels emit `channel_post`
  updates; this relay only subscribes to `message`, so Telegram → mesh would
  never fire.
- **Stop the relay first.** Telegram permits only one `getUpdates` consumer per
  bot, so the script and a running relay would compete for updates.

Group ids are negative, and supergroup ids start with `-100`. If a group is
later upgraded to a supergroup its id changes, so `.env` needs updating.

## Run

```bash
python -m relay
```

You should see `Relay online` posted in the Telegram chat once it connects.

Run it under a process manager to keep it alive (see below). The MeshCore client
is started with auto-reconnect enabled.

## Run as a systemd service (Linux)

A unit file is included: [openhop-telegram-relay.service](openhop-telegram-relay.service).
It assumes the project lives at `/opt/openhop-telegram-relay` and runs as a
dedicated `openhop` user — edit `User`, `Group`, `WorkingDirectory`,
`EnvironmentFile`, and `ExecStart` if your paths differ.

```bash
# create a service user (optional) and place the project
sudo useradd --system --home /opt/openhop-telegram-relay --shell /usr/sbin/nologin openhop
sudo cp -r openhop-telegram-relay /opt/
sudo python3 -m venv /opt/openhop-telegram-relay/.venv
sudo /opt/openhop-telegram-relay/.venv/bin/pip install -r /opt/openhop-telegram-relay/requirements.txt
sudo chown -R openhop:openhop /opt/openhop-telegram-relay

# install and start the service
sudo cp /opt/openhop-telegram-relay/openhop-telegram-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openhop-telegram-relay
```

Manage and inspect it:

```bash
systemctl status openhop-telegram-relay
journalctl -u openhop-telegram-relay -f
```

`.env` is loaded both by systemd (`EnvironmentFile=`) and by the app, so the same
file works whether you run it as a service or by hand. Keep it readable only by
the service user, since it holds the bot token:

```bash
sudo chmod 600 /opt/openhop-telegram-relay/.env
```

> On macOS there is no systemd — use a `launchd` plist (or just run `python -m relay`
> in a `tmux`/`screen` session) instead.

## Reconnection

The relay reconnects on its own and **retries indefinitely**, backing off from
`RECONNECT_MIN_DELAY` up to `RECONNECT_MAX_DELAY`. Restarting or reconfiguring
the OpenHop node, or a network blip, no longer needs a manual restart here.

This is deliberately not left to the `meshcore` library: its own auto-reconnect
makes only `max_reconnect_attempts` tries (default **3**) one second apart, then
emits `DISCONNECTED` with `max_attempts_exceeded` and stops trying for good. Any
outage longer than a few seconds — exactly what a config change on the node
looks like — left the relay silently dead until restarted.

Two things also guard against the failure being invisible:

- **A liveness probe.** Every `HEALTHCHECK_INTERVAL` seconds the node is asked
  for its time. A TCP session can stay open while the node stops answering, so
  waiting for a socket error isn't enough to notice a stall.
- **Chat notifications.** You get `⚠️ Lost the mesh node …` once per outage (not
  once per retry) and `✅ Reconnected to the mesh node.` when it recovers. Set
  `NOTIFY_CONNECTION_EVENTS=false` to keep it to the logs.

Telegram polling is independent of the mesh session, so it keeps running through
an outage. A message sent while the node is down is answered with
`⚠️ Not sent — the mesh node is offline right now.` rather than vanishing.

## Node clock checking

`scripts/sync_node_time.py` asks each configured node for its clock and, where
you've allowed it, corrects it. It's a separate one-shot script meant to run on a
schedule, not part of the relay daemon.

```bash
cp time_sync.example.json time_sync.json   # then edit: names, passwords
chmod 600 time_sync.json                   # it holds admin passwords
python3 scripts/sync_node_time.py --dry-run
```

```json
{
  "tolerance_seconds": 120,
  "nodes": [
    { "name": "Hilltop Repeater",   "password": "…", "set_time": true },
    { "name": "Valley Room Server", "password": "…", "set_time": false }
  ]
}
```

Each node needs its advertised name (or a `pubkey` prefix) and its admin
password — reading the clock over the mesh requires a login. `set_time` is what
"configured for this node" means: `true` lets the script push the correct time,
`false` reports drift and changes nothing. Identify nodes with:

```bash
python3 scripts/sync_node_time.py --dry-run   # reports each configured node
```

Flags: `--dry-run` (never write), `--notify` (post a summary to Telegram),
`--quiet-when-ok` (with `--notify`, only speak up when something needs
attention), `--config` (alternate path). Exit status is `0` when every node is
within tolerance or was corrected and `1` otherwise, so a timer can alert on it.

Output looks like:

```
✅ Hilltop Repeater (+12s)
🕑 Valley Room Server (-3600s) — OK - clock set: 14:23 - 12/3/2025 UTC
⚠️ Ridge Repeater (+900s) — running 900s ahead; firmware refuses to set a clock backwards, so this needs a power cycle at the node
```

### Two firmware limits worth knowing

**A clock that's ahead cannot be fixed remotely.** The firmware accepts
`time <epoch>` only when the value is strictly greater than the node's current
clock, so it will not move a clock backwards ([MeshCore#1332][1332]). The script
detects this and reports it instead of wasting a transmission. Correcting it
means power-cycling the node.

**Drift resolution is one minute.** The `clock` reply is formatted
`HH:MM - D/M/YYYY UTC` with no seconds, so a correctly-set node can read up to
59s behind. Keep `tolerance_seconds` above 60 — the script warns if you don't.

[1332]: https://github.com/meshcore-dev/MeshCore/issues/1332

### Metrics logging

The same run also appends a row per node to a CSV (default `metrics.csv`), for
graphing later:

```csv
timestamp_utc,epoch,node,pubkey,clock_drift_s,battery_mv,battery_pct,voltage_v,current_a,charge_state,temperature_c,…
2026-08-04T12:00:03Z,1785417603,Hilltop Repeater,a3f9c1…,-12,4021,87,4.05,-0.12,discharging,21.5,…
```

Columns come from two requests per node:

- **Status** — `battery_mv`, `uptime_s`, `tx_queue_len`, `noise_floor`,
  `last_rssi`, `last_snr`, `airtime_s`, `rx_airtime_s`, packet counters,
  `recv_errors`.
- **Telemetry** (Cayenne LPP, whatever sensors the node publishes) —
  `temperature_c`, `voltage_v`, `current_a`, `battery_pct`, `illuminance_lux`,
  `power_w`, `humidity_pct`.

`charge_state` is derived from `current_a` (`charging` / `discharging` / `idle`,
blank when the node reports no current). **The sign convention is the node's, not
ours** — this assumes positive current means charge going in. Verify it against a
node you can watch before trusting the column; `current_a` is logged raw so it
can always be re-derived.

Both requests are best-effort and independent. A node that answers one but not
the other still gets a row with the missing fields **blank rather than zero**, so
gaps read as gaps when graphed. `status_ok` and `telemetry_ok` record which half
succeeded. Any LPP reading without a named column (GPS, accelerometer, anything
unrecognised) is preserved verbatim in `telemetry_json`, so nothing is lost.

Metrics need no admin login, so a node listed with **no password and
`set_time: false`** is graphed without its clock being touched — useful for
nodes you don't administer.

Taking that further, the checker can reuse the relay's node store instead of you
listing anything:

```json
{ "metrics_for_known_nodes": true, "metrics_node_types": ["REP", "ROOM"] }
```

Every repeater and room server the relay has discovered then gets sampled each
run. Clocks are never touched this way — only nodes listed explicitly can be
corrected. Adding `"CLI"` sweeps in every handheld that has ever advertised,
which is usually more airtime than it's worth.

The store also makes node references more forgiving: a `name` or `pubkey` prefix
in the config is matched against it when the live contact lookup misses, and a
name that's in the store but absent from the node's contact list produces
"in the node store but not in the node's contact list" rather than a bare
"no contact named …".

```bash
python3 scripts/sync_node_time.py --metrics /var/log/mesh/metrics.csv
python3 scripts/sync_node_time.py --no-metrics   # skip; saves 2 requests/node
```

The header is written once per file. If you upgrade to a version with new
columns, start a new file rather than appending to the old one.

> Airtime cost: metrics add two requests per node per run, on top of the clock
> check. That's the main reason the timer defaults to every 6 hours.

### Running it alongside the relay

The scripts connect on their own endpoint, set by `TIMESYNC_HOST` /
`TIMESYNC_PORT` (or `--host` / `--port`), falling back to `OPENHOP_HOST` /
`OPENHOP_PORT` when unset. Pointing them at a **separate companion endpoint** —
a second port on the node, a proxy, another node — keeps them clear of the relay
entirely:

```bash
TIMESYNC_PORT=5002 python3 scripts/sync_node_time.py --dry-run
```

**Worth knowing if they share one endpoint.** A companion session is effectively
exclusive: messages are *popped* off the device with `SYNC_NEXT_MESSAGE`, so two
connected clients split the queue. The relay can consume a CLI reply the checker
is waiting for (it times out), and the checker can consume channel messages that
should have been relayed (they don't arrive, and nothing logs it). This is the
same reason MeshMonitor's virtual-node server and `meshcore-proxy` exist. The
script logs a warning if it spots a relay on the endpoint it's about to use.

If you'd rather they take turns on one endpoint, `--pause` opts into a handshake:

```bash
python3 scripts/sync_node_time.py --pause
```

1. The relay writes `relay.pid` while running.
2. The checker creates `mesh.pause` and waits.
3. The relay drops its session and writes `relay.released` — real confirmation,
   not a fixed sleep.
4. The checker works, then removes `mesh.pause`.
5. The relay reconnects on its own (it already retries indefinitely).

The chat gets `🔄 Handed the node to a maintenance task; back shortly.` then
`✅ Reconnected`. Telegram polling continues throughout, so messages arriving
during the handover are queued and delivered once the node is back. If the relay
doesn't release within `--pause-timeout` (default 90s) the run **aborts without
changing anything**. `LOCK_DIR` must match between the relay and the script; the
bundled systemd units share `WorkingDirectory`, so they agree by default.

> `scripts/list_nodes.py` also opens a session. Use `--offline` to read the
> store without touching the node, or give it its own `--host` / `--port`.

### Running it on a schedule

`openhop-timesync.service` and `openhop-timesync.timer` are included (every 6
hours by default — each check costs airtime on a shared channel, so don't run it
aggressively):

```bash
sudo cp /opt/openhop-telegram-relay/openhop-timesync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openhop-timesync.timer
systemctl list-timers openhop-timesync.timer
```

Run it once by hand with `sudo systemctl start openhop-timesync.service`. The
unit sets `SuccessExitStatus=0 1` so a node that's out of tolerance is reported
without marking the unit failed.

## Wardriver alerts

Watches a channel (default `wardriving`) and posts when a wardriver turns up
that hasn't been heard for at least `WARDRIVING_QUIET_SECONDS` (default one
hour):

```
🛰 Wardriver seen: asdfasdf (asdfasdf234) 82.323423 48.33434
🛰 Wardriver seen: otherguy
```

Coordinates appear only when the wardriver broadcasts them — MeshMapper only
appends GPS to the on-air message when "Broadcast My Coordinates" is on, so the
second line above is the normal case, not a failure.

The quiet period measures from their **last** transmission, so a wardriver
working an area for an hour produces one alert, not one per ping. They become
newsworthy again after an hour of silence. Timestamps persist in
`WARDRIVING_LOG_FILE`, so restarting the relay doesn't re-announce everyone.

If the node has no such channel, this logs one line at startup and stays off.

### Reading the messages

Channel messages carry **no sender public key** — unlike direct messages, the
payload is just the channel index, text and timing — so the sender has to be
identified from the text itself.

The on-air `#wardriving` format isn't formally specified: MeshMapper describes
it as a short anonymous token, with GPS appended only when the operator opts in.
So the default parser is deliberately loose — it reads a trailing `lat lon` pair
as a position (rejecting out-of-range values rather than guessing at ordering)
and treats the rest as the sender's label, splitting a trailing `(token)` into a
name and id.

If your local traffic looks different, watch it for a bit:

```bash
LOG_LEVEL=DEBUG journalctl -u openhop-telegram-relay -f | grep "wardriving raw"
```

then set a regex with named groups `name`, `id`, `lat`, `lon`:

```bash
WARDRIVING_PATTERN=^WD\|(?P<id>\w+)\|(?P<name>[^|]+)$
```

Messages that don't parse are ignored rather than alerted on.

## New node / repeater alerts

When a node the relay hasn't seen before starts advertising, it posts a line to
the Telegram chat:

```
🗼 New repeater seen: Hilltop North (a3f9c1)
📍 44.65012, -63.59551
```

The location line only appears if the node actually advertises one — nodes with
location sharing off advertise `0,0`, which is reported as no fix rather than as
a point in the Atlantic. Companions, room servers and sensors get their own
label and icon.

Restrict alerts to infrastructure with:

```bash
NOTIFY_NODE_TYPES=repeater,room
```

Nodes filtered out this way are still recorded as seen, so turning a type on
later won't backfill a burst of alerts for nodes already on the mesh.

### Seeing what's known

`seen_nodes.json` records what each node is, not just its key, so it's readable
and reusable:

```bash
python3 scripts/list_nodes.py
```

```
NAME                       TYPE         KEY            LAST ADVERT  WHERE      CLOCK CFG
--------------------------------------------------------------------------------------------
PaulHouse Repeater         repeater     7923698a5f79   12m ago      both       yes
Ridge Room                 room server  ceac785abaca   3h ago       both       -
Paul's phone               companion    e64c4a946b56   1d ago       store      -
```

It joins the store against the node's live contact list, so names and types fill
in even for keys recorded before the store kept metadata. `--offline` skips the
connection, `--json` emits machine-readable output, and
`--time-sync-template` prints a ready-to-edit `nodes` block for the clock
checker (with `set_time: false` so nothing changes until you review it).

Older stores were a bare list of keys; those are migrated automatically on first
load, with names filling in as each node advertises again. Nothing is
re-announced by the upgrade.

Announced nodes are remembered in `SEEN_NODES_FILE` (written atomically), so a
restart doesn't repeat them. **On the very first run the node's existing contact
list is recorded silently** — otherwise you'd get one alert per node the radio
already knows. You get a single `🗂 Tracking N known node(s)` summary instead;
set `ANNOUNCE_SEED_SUMMARY=false` to suppress even that.

Alerts go to Telegram only — nothing is transmitted onto the mesh, so this adds
no RF traffic.

To deliberately re-announce everything, delete the file:

```bash
rm seen_nodes.json
```

### How detection works

Three independent detectors run, because **which push a node emits depends on
its firmware and auto-add setting**:

1. **`NEW_CONTACT`** (`PUSH_CODE_NEW_ADVERT`) — a full contact record for an
   unknown node. Emitted when the node leaves adding contacts to the client.
2. **`ADVERTISEMENT`** — any advert heard, carrying only a public key. A node
   that auto-adds contacts itself announces this way and may **never** emit the
   push above, so relying on `NEW_CONTACT` alone misses new nodes entirely.
   The contact list is consulted to fill in the name and type.
3. **A periodic contact-list diff** every `NODE_POLL_INTERVAL` seconds
   (default 300, `0` disables). This is the guarantee: it catches a new node
   even if no push arrives at all.

Whichever fires first wins; the seen-store keeps it to one alert per node.

If an advert arrives for a node that isn't in the contact list yet and a
`NOTIFY_NODE_TYPES` filter is active, the alert is **deferred** rather than
dropped — the type isn't known yet, and recording it as seen would lose the
alert permanently. It fires once the contact details catch up.

> This reports what your radio hears. A node that never advertises within range
> won't appear.

## How it works

- **Connection** — `MeshCore.create_tcp(host, port)` opens the companion session.
- **Channel resolution** — `commands.get_channel(idx)` is scanned to map
  `MESH_CHANNEL_NAME` to a channel index; incoming messages are filtered to that
  index.
- **mesh → Telegram** — subscribes to `EventType.CHANNEL_MSG_RECV` and forwards
  the text (with a short sender id) via the Telegram `sendMessage` API.
- **Telegram → mesh** — long-polls `getUpdates`; text messages in the configured
  chat are sent with `commands.send_chan_msg(idx, text)`. Bot commands
  (`/…`) are ignored, and only the configured chat is relayed.
- **New nodes** — seeds a seen-set from `commands.get_contacts()`, then
  subscribes to `EventType.NEW_CONTACT` and announces first sightings.
- **Reconnection** — a supervisor loop owns the mesh session: it subscribes to
  `EventType.DISCONNECTED`, probes with `commands.get_time()`, and rebuilds the
  session (re-resolving the channel and re-subscribing) after any drop.
- **Clock checks** — `send_login_sync()` then `send_cmd(dst, "clock")`, with the
  reply read off `EventType.CONTACT_MSG_RECV`; corrections go out as
  `send_cmd(dst, "time <epoch>")`.
- **Metrics** — `req_status_sync()` for battery and radio counters, plus
  `req_telemetry_sync()` for LPP sensor readings, flattened to fixed CSV columns.

## Notes & limits

- LoRa channel payloads are small; messages are truncated to `MESH_MAX_CHARS`.
- The relay skips the Telegram backlog on startup, so messages sent while it was
  offline are not replayed onto the mesh.
- The node must be a member of the channel (hold its key) to decrypt/post to it;
  a bare packet-forwarding repeater won't see channel text.

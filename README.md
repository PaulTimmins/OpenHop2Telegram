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

## Notes & limits

- LoRa channel payloads are small; messages are truncated to `MESH_MAX_CHARS`.
- The relay skips the Telegram backlog on startup, so messages sent while it was
  offline are not replayed onto the mesh.
- The node must be a member of the channel (hold its key) to decrypt/post to it;
  a bare packet-forwarding repeater won't see channel text.

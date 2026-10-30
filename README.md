# voipms-sms-gateway

Send and receive SMS/MMS on a voip.ms DID from a Telegram group. Every external phone number gets its own **forum topic**, and anyone in the (trusted) group can reply in it.

*Not affiliated with VoIP.ms.*

```
Telegram (long polling, outbound only) ─┐
                                         ├─ smsgw (Python, asyncio) ── SQLite
voip.ms URL callback ─► Caddy :443 ──────┘        │
                         (/voipms/<secret> only)  └─► voip.ms REST API
```

- **Inbound:** voip.ms calls the gateway's webhook, which posts the text and photos into the sender's topic. The bot creates the topic automatically the first time a number texts in.
- **Outbound:** anything posted in a topic is sent to that number.
  - Up to 160 characters goes as an SMS.
  - Longer text goes as a single MMS (up to 2048 characters).
  - Photos go as MMS, up to 3 per message, compressed automatically.
- **Status:** the bot reacts 👍 once voip.ms accepts the message. It reacts 👎 and replies with the reason if it fails.
- **Safety net:** every 5 minutes a reconciler pulls recent inbound messages from the API and delivers any the webhook missed.
- **Quota:** a local counter enforces the voip.ms API send limit. The bot warns at 80% and again when the limit is reached.

FreePBX isn't in the SMS path. The gateway just needs to run somewhere on the same LAN, so it shares the public IP you whitelist in voip.ms.

## voip.ms limits to know

| Limit | Value |
|---|---|
| Sends via API (`sendSMS` + `sendMMS`) | **100/day** by default. Ask messaging@voip.ms to raise it, then update `DAILY_SEND_LIMIT` to match. |
| SMS length | 160 characters |
| MMS | 2048 characters and 3 media files, about 1.2 MB each |
| Destinations | US/Canada 10-digit numbers only |
| API request rate | There's an undocumented per-minute cap. The client retries with backoff when it gets `api_limit_exceeded`. |

## Commands

These go in the General topic unless noted.

| Command | What it does |
|---|---|
| `/new 5145551234 [name]` | Start or open a conversation |
| `/name Alice` | Inside a topic: set the contact name, which also renames the topic |
| `/close`, `/reopen` | Inside a topic: archive or reopen it. A new inbound text reopens it automatically. |
| `/contacts` | List conversations with links |
| `/quota` | Show sends used today |
| `/help` | Show help |

Plain messages in General are never sent as SMS.

## Setup

### 1. voip.ms portal

1. **Main Menu → SOAP and REST/JSON API:**
   - Enable the API.
   - Set an **API password**.
   - Add your **public IP** to the allowed IP addresses.
2. **DID Numbers → Manage DIDs → edit your DID → Message Service (SMS/MMS):**
   - Enable SMS/MMS.
   - Enable **URL Callback** and set it to the following URL. Use the placeholder names exactly as listed in the portal's help text, and adjust if they differ.
     ```
     https://sms.example.com/voipms/<WEBHOOK_SECRET>?id={ID}&date={DATE}&from={FROM}&to={TO}&message={MESSAGE}&media={MEDIA}
     ```
   - Enable **URL Callback Retry**. voip.ms then resends every 30 minutes until the gateway answers `ok`.
   - If `{MEDIA}` isn't offered, drop that parameter. The gateway then looks up each inbound message with `getMMS` to fetch any pictures.

### 2. Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a group, open its settings, and turn on **Topics**. This makes it a forum supergroup.
3. Add the bot and make it an **admin** with the **Manage Topics** permission.
   - Admin rights also let the bot see every message, so you don't need to change privacy mode.

### 3. VM (Debian/Ubuntu, Python ≥ 3.10)

```bash
sudo useradd --system --home /opt/smsgw --shell /usr/sbin/nologin smsgw
sudo mkdir -p /opt/smsgw/data && sudo chown -R smsgw:smsgw /opt/smsgw
sudo -u smsgw git clone https://github.com/<you>/voipms-sms-gateway /opt/smsgw/src
sudo -u smsgw python3 -m venv /opt/smsgw/venv
sudo -u smsgw /opt/smsgw/venv/bin/pip install /opt/smsgw/src
sudo -u smsgw cp /opt/smsgw/src/deploy/.env.example /opt/smsgw/.env
sudo chmod 600 /opt/smsgw/.env && sudo -u smsgw nano /opt/smsgw/.env
sudo cp /opt/smsgw/src/deploy/smsgw.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now smsgw
journalctl -u smsgw -f
```

**Finding the chat id:**
1. Leave `TELEGRAM_CHAT_ID=0` and start the service.
2. Post any message in the group. The bot replies with the chat id (`-100…`) and your user id.
3. Put the chat id into `.env` and run `sudo systemctl restart smsgw`.

### 4. Router and Caddy

1. Point a DNS or DDNS name (for example `sms.example.com`) at your public IP.
2. Forward TCP **443** and **80** to the VM. Port 80 is only used for Let's Encrypt.
3. Install Caddy and copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile` with your hostname, then run `sudo systemctl reload caddy`.

Only `/voipms/<secret>` is proxied, and the gateway itself listens on `127.0.0.1` only.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

**Local smoke test** (with `.env` pointing at a test group):

```bash
.venv/bin/python -m smsgw &
curl "http://127.0.0.1:8080/voipms/$WEBHOOK_SECRET?id=test1&from=5145551234&to=$VOIPMS_DID&message=hi&media="
```

A topic named "+1 (514) 555-1234" should appear containing "hi". Running the same curl again changes nothing, because messages are deduplicated by id.

## Things to verify on the first live test

- **Long text as MMS:** a text-only MMS for messages over 160 characters should arrive as one message. If voip.ms rejects it, set `LONG_TEXT_MODE=split`.
- **Callback placeholders:** confirm that `{ID}` and `{MEDIA}` are expanded. Watch the log for `callback without {ID}`.
- **No duplicates:** stop the service, text the DID, then start it again. The reconciler should deliver the message once, within 5 minutes.
  - Deduplication assumes the callback `{ID}` is the same id that `getSMS`/`getMMS` return.
  - If messages show up twice, report it, because that assumption would be wrong.

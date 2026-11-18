# Email Outreach Sender

Safe cold email outreach tool via the Resend API (`https://resend.com`) or custom SMTP with a local web dashboard, multi-language Telegram bot (`EN` / `RU` / `ID`), DNS MX pre-validation, spintax support, and multi-layer anti-spam guardrails.

## Setup

1. Copy `.env.example` to `.env` and fill in your credentials and brand settings:
   ```env
   RESEND_API_KEY=re_your_api_key_here
   SENDER_NAME=Your Brand
   SENDER_EMAIL=hello@yourdomain.com
   REPLY_TO_EMAIL=support@yourdomain.com
   BRAND_NAME=Your Brand
   BRAND_URL=yourdomain.com
   LEADS_DIR=./raw_leads
   DAILY_LIMIT=35
   MIN_DELAY_SEC=90
   MAX_DELAY_SEC=180

   # Optional Telegram bot + IMAP reply bridge
   TG_BOT_TOKEN=
   TG_ACCESS_CODE=7391
   TG_ALLOWED_USERS=
   # remote = local web UI + outbound connect only (when VPS runs bot), auto = stop local bot on 409 conflict
   TG_BOT_MODE=auto
   TG_NOTIFY_CHAT_ID=
   TG_PROXY=
   IMAP_HOST=imap.gmail.com
   IMAP_PORT=993
   IMAP_USER=
   IMAP_PASSWORD=
   ```
2. (Optional) Put raw lead CSV files into `raw_leads/` (or set `LEADS_DIR` in `.env`) and run MX validation:
   ```bash
   python prepare_leads.py
   ```
   This builds `leads_ready.csv` with MX-verified email domains.

## Web Dashboard + Telegram Bot (Combined Mode)

Double-click `start_panel.bat` or run:
```bash
python panel.py
```
This launches:
- Local web dashboard at `http://127.0.0.1:8787` (`RU` / `EN` / `ID` UI switcher)
- Embedded Telegram bot (automatically starts if `TG_BOT_TOKEN` is set in `.env`) sharing the exact same campaign lock, queue, and anti-spam rules

To run the Telegram bot standalone without the web UI:
```bash
python tg_bot.py
```

## 24/7 Server Deployment (Docker)

```bash
git clone https://github.com/estatyq/spamsender.git /var/www/spamsender
cd /var/www/spamsender
cp .env.example .env
nano .env   # fill in RESEND_API_KEY, SENDER_EMAIL, TG_BOT_TOKEN, TG_ACCESS_CODE
mkdir -p data
docker compose up -d --build
docker compose logs -f bot
```
All runtime files (`leads_ready.csv`, `sent_log.json`, `tg_state.json`, `blacklist.txt`, `imap_config.json`) persist inside `./data` on the host.
*(When running the bot 24/7 on a server, set `TG_BOT_MODE=remote` in your local `.env` or launch with `python panel.py --no-bot` so your PC runs only the web dashboard + outbound Telegram connection without competing with the server for `getUpdates`. In `auto` mode, local `panel.py` also automatically stops polling on a `409 Conflict`.)*

### Features & Anti-Spam Guardrails
- Visual queue filtering (`EN` / `ID` / `RU` / `UA`, `no_site` / `has_site`) and live search
- Lead import (`.csv` / `.txt` or pasted lines via web UI or Telegram chat) with automatic DNS MX validation
- Preflight anti-spam text checker (broken braces, unknown variables, spam triggers, ALL CAPS, URL shorteners)
- Forced safe pacing limits (min 45s delay, daily caps for Resend and SMTP)
- Circuit breaker (auto-stops after 3 consecutive send failures)
- Blacklist manager (`blacklist.txt`) and same-day corporate domain cooldown
- Custom SMTP support (e.g. Gmail App Password) alongside Resend API
- Optional IMAP inbox bridge to forward client replies into Telegram and reply in the same email thread

## CLI Usage

```bash
# Send one test email
python mailer.py --test your_email@gmail.com --market EN --site-status no_site

# Preview 3 rendered emails without sending
python mailer.py --dry-run --market EN --site-status no_site

# Run a safe batch (default pauses 90-180s between emails)
python mailer.py --run --market EN --site-status no_site --limit 15
```

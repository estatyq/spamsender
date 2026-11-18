# Email Outreach Sender

Safe cold email outreach tool via the Resend API (`https://resend.com`) or custom SMTP with web dashboard, DNS MX pre-validation, spintax support, and multi-layer anti-spam guardrails.

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
   ```
2. (Optional) Put raw lead CSV files into `raw_leads/` (or set `LEADS_DIR` in `.env`) and run MX validation:
   ```bash
   python prepare_leads.py
   ```
   This builds `leads_ready.csv` with MX-verified email domains.

## Web Dashboard (Recommended)

Double-click `start_panel.bat` or run:
```bash
python panel.py
```
Opens `http://127.0.0.1:8787` in your browser with:
- Visual queue filtering (`EN` / `ID` / `UA`, `no_site` / `has_site`) and live search
- Manual lead import with automatic DNS MX validation
- Preflight anti-spam text checker (broken braces, spam triggers, ALL CAPS, URL shorteners)
- Forced safe pacing limits (min 45s delay, daily caps for Resend and SMTP)
- Circuit breaker (auto-stops after 3 consecutive send failures)
- Blacklist manager (`blacklist.txt`) and same-day corporate domain cooldown
- Custom SMTP support (e.g. Gmail App Password) alongside Resend API

## CLI Usage

```bash
# Send one test email
python mailer.py --test your_email@gmail.com --market EN --site-status no_site

# Preview 3 rendered emails without sending
python mailer.py --dry-run --market EN --site-status no_site

# Run a safe batch (default pauses 90-180s between emails)
python mailer.py --run --market EN --site-status no_site --limit 15
```

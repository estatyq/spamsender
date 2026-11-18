# Email Outreach Sender

Safe cold email outreach tool via the Resend API (`https://resend.com`) with DNS MX pre-validation and spintax support.

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

## Usage

```bash
# Send one test email
python mailer.py --test your_email@gmail.com --market EN --site-status no_site

# Preview 3 rendered emails without sending
python mailer.py --dry-run --market EN --site-status no_site

# Run a safe batch (default pauses 90-180s between emails)
python mailer.py --run --market EN --site-status no_site --limit 15
```

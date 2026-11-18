"""Safe outbound email sender via the Resend API."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LEADS_READY_PATH = BASE_DIR / "leads_ready.csv"
CONTACTS_PATH = BASE_DIR / "contacts.csv"
TEMPLATES_JSON_PATH = BASE_DIR / "templates.json"
TEMPLATE_TXT_PATH = BASE_DIR / "template.txt"
SENT_LOG_PATH = BASE_DIR / "sent_log.json"
RESEND_API_URL = "https://api.resend.com/emails"
SPINTAX_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")


class _SafeFormatDict(dict):
    """Return an empty string for any missing template placeholder."""

    def __missing__(self, key: str) -> str:
        return ""


def load_env(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file into a dict without external deps."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def resolve_spintax(text: str) -> str:
    """Expand {option1|option2} spintax placeholders randomly."""
    while SPINTAX_RE.search(text):
        text = SPINTAX_RE.sub(
            lambda match: random.choice(match.group(1).split("|")),
            text,
        )
    return text


def load_templates_config() -> dict:
    """Load templates.json if present."""
    if not TEMPLATES_JSON_PATH.exists():
        return {}
    return json.loads(TEMPLATES_JSON_PATH.read_text(encoding="utf-8"))


def parse_txt_template(path: Path) -> tuple[str, str]:
    """Read fallback template.txt and return (subject_template, body_template)."""
    if not path.exists():
        raise FileNotFoundError(f"Template file not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].lower().startswith("subject:"):
        raise ValueError("First line of template.txt must start with 'Subject: '")
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    return subject, body


def select_template(
    templates_cfg: dict,
    market: str,
    site_status: str,
    template_id: str | None,
) -> tuple[str, str, str]:
    """Pick (tpl_id, subject_tpl, body_tpl) by template_id or market + site_status."""
    by_key = templates_cfg.get("by_key", {})
    extra = templates_cfg.get("extra", {})

    if template_id:
        for pool in (by_key.values(), extra.values()):
            for item in pool:
                if item.get("id") == template_id:
                    return template_id, item["subject"], item["body"]
        raise ValueError(f"Template ID '{template_id}' not found in templates.json")

    norm_market = (market or "").strip().upper()
    norm_site = (site_status or "").strip().lower()
    if norm_market and by_key:
        key = f"{norm_market}:{norm_site}"
        if key in by_key:
            item = by_key[key]
            return item["id"], item["subject"], item["body"]
        fallback_key = f"{norm_market}:no_site"
        if fallback_key in by_key:
            item = by_key[fallback_key]
            return item["id"], item["subject"], item["body"]

    subject, body = parse_txt_template(TEMPLATE_TXT_PATH)
    return "template.txt", subject, body


def render_message(
    subject_tpl: str,
    body_tpl: str,
    variables: dict[str, str],
) -> tuple[str, str, str]:
    """Render subject, plain text body, and clean minimal HTML body."""
    mapping = _SafeFormatDict(variables)
    subject = resolve_spintax(subject_tpl).format_map(mapping).strip()
    text_body = resolve_spintax(body_tpl).format_map(mapping).strip()
    paragraphs = [p.strip() for p in text_body.split("\n\n") if p.strip()]
    html_paragraphs = [
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>"
        for p in paragraphs
    ]
    html_body = (
        '<div style="font-family: Arial, sans-serif; font-size: 14px; '
        'line-height: 1.5; color: #111111;">'
        + "".join(html_paragraphs)
        + "</div>"
    )
    return subject, text_body, html_body


def load_sent_log(path: Path) -> list[dict]:
    """Load existing sent records from sent_log.json."""
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("sent_log.json must contain a JSON list")
    return data


def save_sent_log(path: Path, records: list[dict]) -> None:
    """Persist sent records to sent_log.json."""
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_email(
    api_key: str,
    from_name: str,
    from_email: str,
    to_email: str,
    reply_to: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> dict:
    """Send a single email via Resend REST API and return parsed JSON response."""
    sender = f"{from_name} <{from_email}>" if from_name else from_email
    payload: dict[str, object] = {
        "from": sender,
        "to": [to_email],
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    req = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "outreach-mailer/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend API error {exc.code}: {error_body}") from exc


def resolve_source_path(source_arg: str | None) -> Path:
    """Pick CSV file from CLI argument, leads_ready.csv, or contacts.csv."""
    if source_arg:
        p = Path(source_arg)
        return p if p.is_absolute() else BASE_DIR / p
    if LEADS_READY_PATH.exists():
        return LEADS_READY_PATH
    return CONTACTS_PATH


def normalize_contact(raw_row: dict[str, str], brand_vars: dict[str, str]) -> dict[str, str]:
    """Normalize column names across leads_ready.csv and custom CSVs."""
    lowered = {
        (k or "").strip().lower(): (v or "").strip()
        for k, v in raw_row.items()
        if k
    }
    email = lowered.get("email", "")
    company = lowered.get("company") or lowered.get("company_name") or "your team"
    name = lowered.get("name") or company
    return {
        **brand_vars,
        **lowered,
        "email": email,
        "company": company,
        "name": name,
        "market": lowered.get("market", "").upper(),
        "site_status": lowered.get("site_status", "no_site").lower(),
    }


def run_test(
    target_email: str,
    api_key: str,
    from_name: str,
    from_email: str,
    reply_to: str,
    brand_vars: dict[str, str],
    market: str,
    site_status: str,
    template_id: str | None,
) -> int:
    """Send one test message using the selected market/template."""
    templates_cfg = load_templates_config()
    chosen_id, subject_tpl, body_tpl = select_template(
        templates_cfg,
        market=market if market != "ALL" else "EN",
        site_status=site_status if site_status != "ALL" else "no_site",
        template_id=template_id,
    )
    subject, text_body, html_body = render_message(
        subject_tpl,
        body_tpl,
        {**brand_vars, "name": "Alex", "company": "Demo Auto Service", "email": target_email},
    )
    print(
        f"Sending test email [{chosen_id}] from '{from_name} <{from_email}>' "
        f"(reply-to: {reply_to or from_email}) to '{target_email}'..."
    )
    result = send_email(
        api_key=api_key,
        from_name=from_name,
        from_email=from_email,
        to_email=target_email,
        reply_to=reply_to,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    print(f"Sent! Resend ID: {result.get('id', 'unknown')}")
    return 0


def run_campaign(
    api_key: str,
    from_name: str,
    from_email: str,
    reply_to: str,
    brand_vars: dict[str, str],
    source_path: Path,
    market_filter: str,
    site_filter: str,
    template_id: str | None,
    daily_limit: int,
    min_delay: int,
    max_delay: int,
    dry_run: bool,
) -> int:
    """Send outreach emails with market/site filtering, daily limit, and pauses."""
    if not source_path.exists():
        raise FileNotFoundError(f"Contacts file not found: {source_path}")

    templates_cfg = load_templates_config()
    sent_log = load_sent_log(SENT_LOG_PATH)
    already_sent = {
        str(item.get("email", "")).strip().lower()
        for item in sent_log
        if item.get("status") == "sent"
    }

    today_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    sent_today = sum(
        1
        for item in sent_log
        if item.get("status") == "sent"
        and str(item.get("sent_at", "")).startswith(today_utc)
    )
    remaining_today = max(0, daily_limit - sent_today)
    if remaining_today == 0 and not dry_run:
        print(f"Daily limit reached ({daily_limit} sent today UTC). Stopping.")
        return 0

    with source_path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    queue: list[dict[str, str]] = []
    for raw_row in rows:
        contact = normalize_contact(raw_row, brand_vars)
        email = contact["email"]
        if not email or email.lower() in already_sent:
            continue
        if market_filter != "ALL" and contact["market"] != market_filter:
            continue
        if site_filter != "ALL" and contact["site_status"] != site_filter:
            continue
        queue.append(contact)

    if not queue:
        print("No matching unsent contacts found.")
        return 0

    batch_size = min(len(queue), 3 if dry_run else remaining_today)
    batch = queue[:batch_size]
    print(
        f"Source: {source_path.name} | Matching unsent: {len(queue)} | "
        f"{'Dry-run preview' if dry_run else 'Batch size'}: {len(batch)} "
        f"(daily_limit={daily_limit}, sent_today={sent_today})"
    )
    print(f"Sender: {from_name} <{from_email}> | Reply-To: {reply_to or from_email}")

    for idx, contact in enumerate(batch, start=1):
        to_email = contact["email"]
        chosen_id, subject_tpl, body_tpl = select_template(
            templates_cfg,
            market=contact["market"],
            site_status=contact["site_status"],
            template_id=template_id,
        )
        subject, text_body, html_body = render_message(subject_tpl, body_tpl, contact)

        if dry_run:
            print(f"\n--- Preview [{idx}/{len(batch)}] template={chosen_id} ---")
            print(f"To: {to_email} ({contact['company']} | {contact['market']} | {contact['site_status']})")
            print(f"Subject: {subject}")
            print(text_body)
            continue

        try:
            res = send_email(
                api_key=api_key,
                from_name=from_name,
                from_email=from_email,
                to_email=to_email,
                reply_to=reply_to,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
            sent_log.append(
                {
                    "email": to_email,
                    "company": contact["company"],
                    "market": contact["market"],
                    "template": chosen_id,
                    "status": "sent",
                    "id": res.get("id"),
                    "from": f"{from_name} <{from_email}>",
                    "subject": subject,
                    "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            save_sent_log(SENT_LOG_PATH, sent_log)
            already_sent.add(to_email.lower())
            print(f"[{idx}/{len(batch)}] Sent ({chosen_id}) to {to_email} (id={res.get('id')})")
        except Exception as exc:  # noqa: BLE001
            sent_log.append(
                {
                    "email": to_email,
                    "status": "error",
                    "error": str(exc),
                    "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            save_sent_log(SENT_LOG_PATH, sent_log)
            print(f"[{idx}/{len(batch)}] Failed for {to_email}: {exc}", file=sys.stderr)

        if idx < len(batch):
            delay = random.randint(min_delay, max(min_delay, max_delay))
            print(f"Waiting {delay}s before next email...")
            time.sleep(delay)

    print("Run finished.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Safe email outreach sender via the Resend API",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--test",
        metavar="EMAIL",
        help="Send a single test email to EMAIL and exit",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Run outreach campaign for contacts in CSV",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview 3 rendered emails without sending anything",
    )
    parser.add_argument("--csv", help="Path to CSV file (default: leads_ready.csv)")
    parser.add_argument(
        "--market",
        default="ALL",
        choices=["ALL", "EN", "ID", "UA"],
        help="Filter contacts by market (or pick market for --test)",
    )
    parser.add_argument(
        "--site-status",
        default="ALL",
        choices=["ALL", "no_site", "has_site"],
        help="Filter by website status (or pick template variant for --test)",
    )
    parser.add_argument("--template-id", help="Force a specific template id from templates.json")
    parser.add_argument("--limit", type=int, help="Override DAILY_LIMIT for this run")
    parser.add_argument("--from-email", help="Override sender email address")
    parser.add_argument("--from-name", help="Override sender display name")
    return parser


def main() -> int:
    """Entry point for CLI execution."""
    parser = build_parser()
    args = parser.parse_args()

    env = {**load_env(ENV_PATH), **os.environ}
    api_key = env.get("RESEND_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        print("Error: RESEND_API_KEY is not set in .env or environment.", file=sys.stderr)
        return 1

    from_name = (args.from_name or env.get("SENDER_NAME") or "Our Team").strip()
    from_email = (args.from_email or env.get("SENDER_EMAIL") or "").strip()
    if not from_email and not args.dry_run:
        print("Error: SENDER_EMAIL is not set in .env or --from-email.", file=sys.stderr)
        return 1
    if not from_email:
        from_email = "hello@yourdomain.com"

    reply_to = env.get("REPLY_TO_EMAIL", "").strip()
    brand_vars = {
        "brand_name": (env.get("BRAND_NAME") or from_name).strip(),
        "brand_url": (env.get("BRAND_URL") or from_email.split("@")[-1]).strip(),
    }
    daily_limit = args.limit if args.limit is not None else int(env.get("DAILY_LIMIT", "35"))
    min_delay = int(env.get("MIN_DELAY_SEC", "90"))
    max_delay = int(env.get("MAX_DELAY_SEC", "180"))

    if args.test:
        return run_test(
            target_email=args.test.strip(),
            api_key=api_key,
            from_name=from_name,
            from_email=from_email,
            reply_to=reply_to,
            brand_vars=brand_vars,
            market=args.market,
            site_status=args.site_status,
            template_id=args.template_id,
        )

    return run_campaign(
        api_key=api_key,
        from_name=from_name,
        from_email=from_email,
        reply_to=reply_to,
        brand_vars=brand_vars,
        source_path=resolve_source_path(args.csv),
        market_filter=args.market,
        site_filter=args.site_status,
        template_id=args.template_id,
        daily_limit=daily_limit,
        min_delay=min_delay,
        max_delay=max_delay,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())

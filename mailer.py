"""Safe outbound email sender via Resend API or SMTP with anti-spam guardrails."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from email.message import EmailMessage
from email.utils import formataddr
import html
import json
import os
from pathlib import Path
import random
import re
import smtplib
import ssl
import sys
import threading
import time
from typing import Callable
import urllib.error
import urllib.request

from guardrails import (
    check_message_safety,
    enforce_safe_pacing,
    is_blacklisted,
    load_blacklist,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ACCOUNTS_PATH = BASE_DIR / "accounts.json"
LEADS_READY_PATH = BASE_DIR / "leads_ready.csv"
CONTACTS_PATH = BASE_DIR / "contacts.csv"
TEMPLATES_JSON_PATH = BASE_DIR / "templates.json"
TEMPLATE_TXT_PATH = BASE_DIR / "template.txt"
SENT_LOG_PATH = BASE_DIR / "sent_log.json"
RESEND_API_URL = "https://api.resend.com/emails"
SPINTAX_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")

PUBLIC_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.co.id",
    "hotmail.com",
    "hotmail.co.uk",
    "outlook.com",
    "live.com",
    "icloud.com",
    "ukr.net",
    "i.ua",
    "meta.ua",
    "btinternet.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "bellsouth.net",
}
CONSECUTIVE_ERROR_STOP_LIMIT = 3


class _SafeFormatDict(dict):
    """Return an empty string for any missing template placeholder."""

    def __missing__(self, key: str) -> str:
        return ""


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
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


def load_smtp_accounts(path: Path = ACCOUNTS_PATH) -> list[dict]:
    """Load locally stored SMTP sender accounts from accounts.json."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def save_smtp_accounts(accounts: list[dict], path: Path = ACCOUNTS_PATH) -> None:
    """Persist local SMTP sender accounts to accounts.json."""
    path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_spintax(text: str) -> str:
    """Expand {option1|option2} spintax placeholders randomly."""
    while SPINTAX_RE.search(text):
        text = SPINTAX_RE.sub(
            lambda match: random.choice(match.group(1).split("|")),
            text,
        )
    return text


def load_templates_config(path: Path = TEMPLATES_JSON_PATH) -> dict:
    """Load templates.json if present."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_templates_config(cfg: dict, path: Path = TEMPLATES_JSON_PATH) -> None:
    """Save templates.json."""
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_txt_template(path: Path = TEMPLATE_TXT_PATH) -> tuple[str, str]:
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


def load_sent_log(path: Path = SENT_LOG_PATH) -> list[dict]:
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


def save_sent_log(records: list[dict], path: Path = SENT_LOG_PATH) -> None:
    """Persist sent records to sent_log.json."""
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_via_resend(
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


def send_via_smtp(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_name: str,
    from_email: str,
    to_email: str,
    reply_to: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> dict:
    """Send a single email via SMTP SSL (465) or STARTTLS (587)."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    context = ssl.create_default_context()
    if int(smtp_port) == 465:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=25, context=context) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=25) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

    return {"id": f"smtp:{smtp_host}:{int(time.time())}"}


def test_smtp_connection(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
) -> None:
    """Verify SMTP credentials without sending an email."""
    context = ssl.create_default_context()
    if int(smtp_port) == 465:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=15, context=context) as server:
            server.login(smtp_user, smtp_password)
    else:
        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_password)


def send_via_account(
    account: dict,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> dict:
    """Dispatch email using either a Resend or SMTP account dictionary."""
    provider = account.get("provider", "resend")
    from_name = (account.get("sender_name") or "").strip()
    from_email = (account.get("sender_email") or "").strip()
    reply_to = (account.get("reply_to_email") or "").strip()

    if provider == "smtp":
        return send_via_smtp(
            smtp_host=account["smtp_host"],
            smtp_port=int(account.get("smtp_port", 465)),
            smtp_user=account["smtp_user"],
            smtp_password=account["smtp_password"],
            from_name=from_name,
            from_email=from_email,
            to_email=to_email,
            reply_to=reply_to,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    return send_via_resend(
        api_key=account["api_key"],
        from_name=from_name,
        from_email=from_email,
        to_email=to_email,
        reply_to=reply_to,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


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


def build_default_resend_account(
    env: dict[str, str],
    from_name: str | None = None,
    from_email: str | None = None,
) -> dict:
    """Build Resend account dict from .env and optional overrides."""
    sender_name = (from_name or env.get("SENDER_NAME") or "Our Team").strip()
    sender_email = (from_email or env.get("SENDER_EMAIL") or "").strip()
    return {
        "id": "resend_env",
        "label": f"Resend ({sender_email or 'not set'})",
        "provider": "resend",
        "api_key": env.get("RESEND_API_KEY", "").strip(),
        "sender_name": sender_name,
        "sender_email": sender_email,
        "reply_to_email": env.get("REPLY_TO_EMAIL", "").strip(),
    }


def get_sent_stats(sent_log: list[dict], source_sender: str | None = None) -> dict[str, int]:
    """Return total sent and UTC today's sent count."""
    today_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    total_sent = 0
    sent_today = 0
    for item in sent_log:
        if item.get("status") != "sent":
            continue
        if source_sender and source_sender not in str(item.get("from", "")):
            continue
        total_sent += 1
        if str(item.get("sent_at", "")).startswith(today_utc):
            sent_today += 1
    return {"total_sent": total_sent, "sent_today": sent_today}


def filter_queue(
    source_path: Path,
    brand_vars: dict[str, str],
    market_filter: str = "ALL",
    site_filter: str = "ALL",
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Build deduplicated, blacklist-checked queue from CSV."""
    sent_log = load_sent_log(SENT_LOG_PATH)
    blacklist = load_blacklist()
    already_sent = {
        str(item.get("email", "")).strip().lower()
        for item in sent_log
        if item.get("status") == "sent"
    }
    today_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    sent_domains_today = {
        str(item.get("email", "")).strip().lower().split("@", 1)[1]
        for item in sent_log
        if item.get("status") == "sent"
        and "@" in str(item.get("email", ""))
        and str(item.get("sent_at", "")).startswith(today_utc)
    }

    norm_market = (market_filter or "ALL").strip().upper()
    raw_site = (site_filter or "ALL").strip().lower()
    norm_site = "ALL" if raw_site in ("", "all") else raw_site

    with source_path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    queue: list[dict[str, str]] = []
    seen_in_file: set[str] = set()
    skipped_sent = 0
    skipped_blacklist = 0
    skipped_domain_cooldown = 0

    for raw_row in rows:
        contact = normalize_contact(raw_row, brand_vars)
        email = contact["email"].lower()
        if not email or "@" not in email or email in seen_in_file:
            continue
        seen_in_file.add(email)

        if norm_market != "ALL" and contact["market"] != norm_market:
            continue
        if norm_site != "ALL" and contact["site_status"] != norm_site:
            continue
        if email in already_sent:
            skipped_sent += 1
            continue
        if is_blacklisted(email, blacklist):
            skipped_blacklist += 1
            continue

        domain = email.split("@", 1)[1]
        if domain not in PUBLIC_EMAIL_DOMAINS and domain in sent_domains_today:
            skipped_domain_cooldown += 1
            continue

        queue.append(contact)

    stats = {
        "total_rows": len(rows),
        "skipped_sent": skipped_sent,
        "skipped_blacklist": skipped_blacklist,
        "skipped_domain_cooldown": skipped_domain_cooldown,
        "queued": len(queue),
    }
    return queue, stats


def execute_campaign(
    account: dict,
    brand_vars: dict[str, str],
    source_path: Path,
    market_filter: str = "ALL",
    site_filter: str = "ALL",
    template_id: str | None = None,
    daily_limit: int = 35,
    min_delay: int = 90,
    max_delay: int = 180,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
    log_cb: Callable[[str], None] = print,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """Run outreach batch with anti-spam preflight checks, pacing clamp, and circuit breaker."""
    if not source_path.exists():
        raise FileNotFoundError(f"Contacts file not found: {source_path}")

    provider = account.get("provider", "resend")
    safe_limit, safe_min, safe_max, notices = enforce_safe_pacing(
        provider, daily_limit, min_delay, max_delay
    )
    for note in notices:
        log_cb(f"[Защита от спама] {note}")

    templates_cfg = load_templates_config()
    sent_log = load_sent_log(SENT_LOG_PATH)
    today_stats = get_sent_stats(sent_log)
    sent_today = today_stats["sent_today"]
    remaining_today = max(0, safe_limit - sent_today)
    if remaining_today == 0 and not dry_run:
        msg = f"Дневной лимит уже достигнут ({sent_today}/{safe_limit} за сегодня UTC). Отправка остановлена."
        log_cb(msg)
        return {"ok": False, "sent": 0, "errors": 0, "reason": msg}

    queue, q_stats = filter_queue(
        source_path=source_path,
        brand_vars=brand_vars,
        market_filter=market_filter,
        site_filter=site_filter,
    )
    if not queue:
        msg = "Подходящих неотправленных контактов не найдено."
        log_cb(msg)
        return {"ok": True, "sent": 0, "errors": 0, "reason": msg}

    batch_size = min(len(queue), 3 if dry_run else remaining_today)
    batch = queue[:batch_size]

    # Preflight safety check on first contact before sending anything!
    first_contact = batch[0]
    first_tpl_id, first_subj_tpl, first_body_tpl = select_template(
        templates_cfg,
        market=first_contact["market"],
        site_status=first_contact["site_status"],
        template_id=template_id,
    )
    sample_subj, sample_text, _ = render_message(first_subj_tpl, first_body_tpl, first_contact)
    safety = check_message_safety(first_subj_tpl, first_body_tpl, sample_subj, sample_text)
    if not safety["ok"] and not dry_run:
        reason = " | ".join(safety["errors"])  # type: ignore[arg-type]
        log_cb(f"[БЛОКИРОВКА АНТИ-СПАМ] {reason}")
        return {"ok": False, "sent": 0, "errors": 0, "reason": reason, "safety": safety}
    for warn in safety["warnings"]:  # type: ignore[attr-defined]
        log_cb(f"[Предупреждение анти-спам] {warn}")

    from_display = f"{account.get('sender_name', '')} <{account.get('sender_email', '')}>".strip()
    log_cb(
        f"Источник: {source_path.name} | Подходящих: {q_stats['queued']} | "
        f"{'Предпросмотр' if dry_run else 'К отправке'}: {len(batch)} "
        f"(лимит={safe_limit}, сегодня уже={sent_today}, пауза={safe_min}-{safe_max}с)"
    )
    log_cb(f"Отправитель ({provider}): {from_display} | Ответы: {account.get('reply_to_email') or account.get('sender_email')}")

    sent_count = 0
    error_count = 0
    consecutive_errors = 0
    sent_domains_this_run: set[str] = set()

    for idx, contact in enumerate(batch, start=1):
        if stop_event and stop_event.is_set():
            log_cb("Остановлено пользователем.")
            break

        to_email = contact["email"]
        domain = to_email.lower().split("@", 1)[1]
        if domain not in PUBLIC_EMAIL_DOMAINS and domain in sent_domains_this_run and not dry_run:
            log_cb(f"[{idx}/{len(batch)}] Пропуск {to_email}: домен {domain} уже получал письмо в этой волне.")
            continue

        chosen_id, subject_tpl, body_tpl = select_template(
            templates_cfg,
            market=contact["market"],
            site_status=contact["site_status"],
            template_id=template_id,
        )
        subject, text_body, html_body = render_message(subject_tpl, body_tpl, contact)
        msg_safety = check_message_safety(subject_tpl, body_tpl, subject, text_body)
        if not msg_safety["ok"] and not dry_run:
            log_cb(f"[{idx}/{len(batch)}] Остановлено защитой для {to_email}: {'; '.join(msg_safety['errors'])}")
            break

        if dry_run:
            log_cb(
                f"\n--- Preview [{idx}/{len(batch)}] template={chosen_id} ---\n"
                f"To: {to_email} ({contact['company']} | {contact['market']} | {contact['site_status']})\n"
                f"Subject: {subject}\n{text_body}"
            )
            continue

        try:
            res = send_via_account(
                account=account,
                to_email=to_email,
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
                    "provider": provider,
                    "status": "sent",
                    "id": res.get("id"),
                    "from": from_display,
                    "subject": subject,
                    "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            save_sent_log(sent_log, SENT_LOG_PATH)
            sent_domains_this_run.add(domain)
            sent_count += 1
            consecutive_errors = 0
            if progress_cb:
                progress_cb(idx, len(batch))
            log_cb(f"[{idx}/{len(batch)}] Отправлено ({chosen_id}) -> {to_email} (id={res.get('id')})")
        except Exception as exc:  # noqa: BLE001
            error_count += 1
            consecutive_errors += 1
            sent_log.append(
                {
                    "email": to_email,
                    "provider": provider,
                    "status": "error",
                    "error": str(exc),
                    "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            save_sent_log(sent_log, SENT_LOG_PATH)
            log_cb(f"[{idx}/{len(batch)}] Ошибка для {to_email}: {exc}")
            if consecutive_errors >= CONSECUTIVE_ERROR_STOP_LIMIT:
                log_cb(
                    f"[АВАРИЙНЫЙ СТОП] {CONSECUTIVE_ERROR_STOP_LIMIT} ошибки подряд — "
                    "рассылка остановлена автоматически для защиты репутации домена."
                )
                break

        if idx < len(batch):
            delay = random.randint(safe_min, safe_max)
            log_cb(f"Пауза {delay}с перед следующим письмом...")
            if stop_event:
                if stop_event.wait(delay):
                    log_cb("Остановлено пользователем во время паузы.")
                    break
            else:
                time.sleep(delay)

    log_cb(f"Завершено: отправлено {sent_count}, ошибок {error_count}.")
    return {"ok": True, "sent": sent_count, "errors": error_count}


def run_test(
    target_email: str,
    account: dict,
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
        f"Sending test email [{chosen_id}] via {account['provider']} from "
        f"'{account['sender_name']} <{account['sender_email']}>' to '{target_email}'..."
    )
    result = send_via_account(
        account=account,
        to_email=target_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    print(f"Sent! ID: {result.get('id', 'unknown')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Safe email outreach sender via Resend API or SMTP",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test", metavar="EMAIL", help="Send a single test email to EMAIL")
    mode.add_argument("--run", action="store_true", help="Run outreach campaign")
    mode.add_argument("--dry-run", action="store_true", help="Preview 3 emails without sending")
    parser.add_argument("--csv", help="Path to CSV file (default: leads_ready.csv)")
    parser.add_argument("--market", default="ALL", choices=["ALL", "EN", "ID", "UA"])
    parser.add_argument("--site-status", default="ALL", choices=["ALL", "no_site", "has_site"])
    parser.add_argument("--template-id", help="Force template id from templates.json")
    parser.add_argument("--limit", type=int, help="Override DAILY_LIMIT for this run")
    parser.add_argument("--from-email", help="Override sender email address")
    parser.add_argument("--from-name", help="Override sender display name")
    return parser


def main() -> int:
    """Entry point for CLI execution."""
    parser = build_parser()
    args = parser.parse_args()

    env = {**load_env(ENV_PATH), **os.environ}
    account = build_default_resend_account(env, args.from_name, args.from_email)
    if not account["api_key"] and not args.dry_run:
        print("Error: RESEND_API_KEY is not set in .env or environment.", file=sys.stderr)
        return 1
    if not account["sender_email"] and not args.dry_run:
        print("Error: SENDER_EMAIL is not set in .env or --from-email.", file=sys.stderr)
        return 1
    if not account["sender_email"]:
        account["sender_email"] = "hello@yourdomain.com"

    brand_vars = {
        "brand_name": (env.get("BRAND_NAME") or account["sender_name"]).strip(),
        "brand_url": (env.get("BRAND_URL") or account["sender_email"].split("@")[-1]).strip(),
    }
    daily_limit = args.limit if args.limit is not None else int(env.get("DAILY_LIMIT", "35"))
    min_delay = int(env.get("MIN_DELAY_SEC", "90"))
    max_delay = int(env.get("MAX_DELAY_SEC", "180"))

    if args.test:
        return run_test(
            target_email=args.test.strip(),
            account=account,
            brand_vars=brand_vars,
            market=args.market,
            site_status=args.site_status,
            template_id=args.template_id,
        )

    res = execute_campaign(
        account=account,
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
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

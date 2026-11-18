"""Shared runtime service layer for web panel and Telegram bot."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import datetime as dt
import email as email_pkg
from email.header import decode_header
from email.utils import parseaddr
import html as html_lib
import imaplib
import json
import os
from pathlib import Path
import re
import threading

from guardrails import (
    check_message_safety,
    enforce_safe_pacing,
)
from mailer import (
    BASE_DIR,
    CONTACTS_PATH,
    ENV_PATH,
    LEADS_READY_PATH,
    build_default_resend_account,
    execute_campaign,
    load_env,
    load_smtp_accounts,
    load_templates_config,
    render_html_from_text,
    render_message,
    resolve_source_path,
    select_template,
    send_via_account,
)
from prepare_leads import check_domain_mx, is_clean_syntax

IMAP_CONFIG_PATH = BASE_DIR / "imap_config.json"


class CampaignState:
    """Thread-safe holder shared between the web panel and Telegram bot."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stop_event = threading.Event()
        self.progress_current = 0
        self.progress_total = 0
        self.logs: list[str] = []
        self.last_result: dict | None = None

    def append_log(self, message: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{ts}] {message}")
            if len(self.logs) > 250:
                self.logs = self.logs[-250:]

    def set_progress(self, current: int, total: int) -> None:
        with self.lock:
            self.progress_current = current
            self.progress_total = total

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "running": self.running,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "logs": list(self.logs[-80:]),
                "last_result": self.last_result,
            }


STATE = CampaignState()


def get_env() -> dict[str, str]:
    """Read merged .env and process environment."""
    return {**load_env(ENV_PATH), **os.environ}


def brand_vars(env: dict[str, str], sender_name: str, sender_email: str) -> dict[str, str]:
    """Resolve brand placeholders from .env or sender identity."""
    return {
        "brand_name": (env.get("BRAND_NAME") or sender_name or "Our Team").strip(),
        "brand_url": (
            env.get("BRAND_URL")
            or (sender_email.split("@")[-1] if "@" in sender_email else "yourdomain.com")
        ).strip(),
    }


def all_accounts(env: dict[str, str]) -> list[dict]:
    """Return all configured Resend and SMTP sender accounts."""
    accounts: list[dict] = []
    resend_acc = build_default_resend_account(env)
    if resend_acc["api_key"]:
        accounts.append(resend_acc)
    accounts.extend(load_smtp_accounts())
    return accounts


def mask_account(acc: dict) -> dict:
    """Return account metadata without secret API keys or passwords."""
    return {
        "id": acc.get("id"),
        "label": acc.get("label") or acc.get("sender_email"),
        "provider": acc.get("provider", "resend"),
        "sender_name": acc.get("sender_name", ""),
        "sender_email": acc.get("sender_email", ""),
        "reply_to_email": acc.get("reply_to_email", ""),
        "smtp_host": acc.get("smtp_host", ""),
        "smtp_port": acc.get("smtp_port", 465),
    }


def find_account(account_id: str | None, env: dict[str, str]) -> dict:
    """Find sender account by id or default to first configured account."""
    accounts = all_accounts(env)
    if not accounts:
        raise ValueError("No sender account configured in .env or accounts.json.")
    if not account_id:
        return accounts[0]
    for acc in accounts:
        if acc.get("id") == account_id:
            return acc
    return accounts[0]


def csv_sources() -> list[dict[str, object]]:
    """Return available CSV sources with row counts."""
    items: list[dict[str, object]] = []
    for path in (LEADS_READY_PATH, CONTACTS_PATH):
        if path.exists():
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                count = sum(1 for _ in csv.DictReader(fh))
            items.append({"name": path.name, "count": count})
    return items


def import_leads_text(
    raw_text: str,
    target_name: str = "contacts.csv",
    market: str = "EN",
    site_status: str = "no_site",
) -> dict[str, object]:
    """Parse pasted emails or CSV rows, run DNS MX validation, and append cleanly."""
    clean_text = raw_text.strip()
    if not clean_text:
        raise ValueError("Empty input.")

    target_path = CONTACTS_PATH if target_name == "contacts.csv" else LEADS_READY_PATH
    lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]
    parsed_rows: list[dict[str, str]] = []

    if lines and ("email" in lines[0].lower() and ("," in lines[0] or ";" in lines[0])):
        delim = ";" if lines[0].count(";") > lines[0].count(",") else ","
        for row in csv.DictReader(lines, delimiter=delim):
            em = (row.get("email") or row.get("Email") or "").strip().lower()
            if em:
                parsed_rows.append(
                    {
                        "email": em,
                        "company": (row.get("company") or row.get("Company_Name") or row.get("name") or "your team").strip(),
                        "market": (row.get("market") or row.get("Market") or market).strip().upper(),
                        "country": (row.get("country") or row.get("Country") or "").strip(),
                        "city": (row.get("city") or row.get("City") or "").strip(),
                        "niche": (row.get("niche") or row.get("Niche") or "").strip(),
                        "site_status": (row.get("site_status") or row.get("Site_Status") or site_status).strip().lower(),
                        "site_url": (row.get("site_url") or row.get("Site_URL") or "").strip(),
                        "phone": (row.get("phone") or row.get("Phone_Formatted") or "").strip(),
                        "source": "manual_import",
                    }
                )
    else:
        for ln in lines:
            parts = [p.strip() for p in ln.replace(";", ",").split(",")]
            em = parts[0].lower()
            company = parts[1] if len(parts) > 1 and parts[1] else "your team"
            parsed_rows.append(
                {
                    "email": em,
                    "company": company,
                    "market": market.upper(),
                    "country": "",
                    "city": "",
                    "niche": "",
                    "site_status": site_status.lower(),
                    "site_url": "",
                    "phone": "",
                    "source": "manual_import",
                }
            )

    existing: list[dict[str, str]] = []
    existing_emails: set[str] = set()
    if target_path.exists():
        with target_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                em = (row.get("email") or "").strip().lower()
                if em:
                    existing_emails.add(em)
                existing.append(row)

    candidates: list[dict[str, str]] = []
    duplicates = 0
    invalid_syntax = 0
    for row in parsed_rows:
        em = row["email"]
        if em in existing_emails or any(c["email"] == em for c in candidates):
            duplicates += 1
            continue
        ok, _ = is_clean_syntax(em)
        if not ok:
            invalid_syntax += 1
            continue
        candidates.append(row)

    domains = sorted({c["email"].split("@", 1)[1] for c in candidates})
    mx_map: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(check_domain_mx, dom) for dom in domains]
        for fut in as_completed(futs):
            dom, has_mx, _ = fut.result()
            mx_map[dom] = has_mx

    added = 0
    invalid_mx = 0
    for item in candidates:
        dom = item["email"].split("@", 1)[1]
        if not mx_map.get(dom, False):
            invalid_mx += 1
            continue
        existing.append(item)
        added += 1

    fieldnames = [
        "email",
        "company",
        "market",
        "country",
        "city",
        "niche",
        "site_status",
        "site_url",
        "phone",
        "source",
    ]
    with target_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in existing:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    return {
        "ok": True,
        "target": target_path.name,
        "added": added,
        "duplicates": duplicates,
        "invalid_syntax": invalid_syntax,
        "invalid_mx": invalid_mx,
    }


def start_campaign_async(
    account_id: str | None = None,
    source_name: str | None = None,
    market: str = "ALL",
    site_status: str = "ALL",
    template_id: str | None = None,
    daily_limit: int | None = None,
    min_delay: int | None = None,
    max_delay: int | None = None,
    on_complete=None,
) -> dict[str, object]:
    """Acquire shared lock and start background campaign with full anti-spam guardrails."""
    with STATE.lock:
        if STATE.running:
            raise ValueError("Campaign is already running.")
        STATE.running = True
        STATE.stop_event.clear()
        STATE.progress_current = 0
        STATE.progress_total = 0
        STATE.last_result = None
        STATE.logs = []

    env = get_env()
    try:
        account = find_account(account_id, env)
        bvars = brand_vars(env, account["sender_name"], account["sender_email"])
        source_path = resolve_source_path(source_name)
        norm_market = (market or "ALL").upper()
        raw_site = (site_status or "ALL").lower()
        norm_site = "ALL" if raw_site in ("", "all") else raw_site
        req_limit = int(daily_limit if daily_limit is not None else env.get("DAILY_LIMIT", "35"))
        req_min = int(min_delay if min_delay is not None else env.get("MIN_DELAY_SEC", "90"))
        req_max = int(max_delay if max_delay is not None else env.get("MAX_DELAY_SEC", "180"))
        safe_limit, safe_min, safe_max, _ = enforce_safe_pacing(
            account.get("provider", "resend"), req_limit, req_min, req_max
        )
    except Exception:
        with STATE.lock:
            STATE.running = False
        raise

    def _worker() -> None:
        res: dict[str, object]
        try:
            res = execute_campaign(
                account=account,
                brand_vars=bvars,
                source_path=source_path,
                market_filter=norm_market,
                site_filter=norm_site,
                template_id=template_id,
                daily_limit=safe_limit,
                min_delay=safe_min,
                max_delay=safe_max,
                dry_run=False,
                stop_event=STATE.stop_event,
                log_cb=STATE.append_log,
                progress_cb=STATE.set_progress,
            )
        except Exception as exc:  # noqa: BLE001
            STATE.append_log(f"[ERROR] {exc}")
            res = {"ok": False, "error": str(exc)}
        finally:
            with STATE.lock:
                STATE.last_result = res
                STATE.running = False
        if on_complete:
            try:
                on_complete(res)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "limit": safe_limit, "min_delay": safe_min, "max_delay": safe_max}


def preview_template_safety(
    market: str = "EN",
    site_status: str = "no_site",
    template_id: str | None = None,
    custom_subject: str | None = None,
    custom_body: str | None = None,
) -> dict[str, object]:
    """Render sample message and run check_message_safety."""
    env = get_env()
    bvars = brand_vars(env, env.get("SENDER_NAME", ""), env.get("SENDER_EMAIL", ""))
    templates_cfg = load_templates_config()
    if custom_subject is not None and custom_body is not None:
        chosen_id = template_id or "custom"
        subj_tpl, body_tpl = custom_subject, custom_body
    else:
        chosen_id, subj_tpl, body_tpl = select_template(
            templates_cfg,
            market=market if market != "ALL" else "EN",
            site_status=site_status if site_status != "ALL" else "no_site",
            template_id=template_id,
        )
    sample_vars = {
        **bvars,
        "name": "Alex",
        "company": "Demo Auto Service",
        "email": "client@example.org",
    }
    rendered_subj, rendered_body, _ = render_message(subj_tpl, body_tpl, sample_vars)
    safety = check_message_safety(subj_tpl, body_tpl, rendered_subj, rendered_body)
    return {
        "template_id": chosen_id,
        "rendered_subject": rendered_subj,
        "rendered_body": rendered_body,
        "safety": safety,
    }


def load_imap_config() -> dict[str, str]:
    """Load IMAP settings from imap_config.json merged with .env defaults."""
    env = get_env()
    cfg = {
        "host": env.get("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com",
        "port": env.get("IMAP_PORT", "993").strip() or "993",
        "user": env.get("IMAP_USER", "").strip(),
        "password": env.get("IMAP_PASSWORD", "").strip(),
    }
    if IMAP_CONFIG_PATH.exists():
        stored = json.loads(IMAP_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            for k in ("host", "port", "user", "password"):
                if stored.get(k):
                    cfg[k] = str(stored[k]).strip()
    return cfg


def save_imap_config(host: str, port: int, user: str, password: str) -> None:
    """Validate IMAP login and save to imap_config.json."""
    test_imap_connection(host, port, user, password)
    IMAP_CONFIG_PATH.write_text(
        json.dumps(
            {
                "host": host.strip(),
                "port": str(port),
                "user": user.strip(),
                "password": password.strip(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_imap_connection(host: str, port: int, user: str, password: str) -> None:
    """Test IMAP SSL login cleanly."""
    with imaplib.IMAP4_SSL(host, int(port), timeout=15) as conn:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _extract_email_text(msg) -> str:
    if msg.is_multipart():
        html_fallback = ""
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                return text.strip()
            if ctype == "text/html" and not html_fallback:
                html_fallback = re.sub(r"<[^>]+>", " ", text)
                html_fallback = html_lib.unescape(re.sub(r"\s+", " ", html_fallback)).strip()
        return html_fallback

    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if msg.get_content_type() == "text/html":
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    return text.strip()


def fetch_new_imap_replies(last_uid: int) -> tuple[int, list[dict[str, str]]]:
    """Fetch new INBOX messages with UID > last_uid without flooding on initial sync."""
    cfg = load_imap_config()
    if not cfg.get("user") or not cfg.get("password"):
        return last_uid, []

    with imaplib.IMAP4_SSL(cfg["host"], int(cfg["port"]), timeout=20) as conn:
        conn.login(cfg["user"], cfg["password"])
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return last_uid, []
        uids = [int(u) for u in data[0].split() if u.isdigit()]
        if not uids:
            return last_uid, []

        max_uid = max(uids)
        if last_uid <= 0:
            # Initialize cursor at current latest UID so old inbox mail is never replayed.
            return max_uid, []

        new_uids = [u for u in uids if u > last_uid][-10:]
        replies: list[dict[str, str]] = []
        for uid in new_uids:
            ftyp, msg_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            if ftyp != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email_pkg.message_from_bytes(msg_data[0][1])
            raw_from = _decode_mime_header(msg.get("From"))
            sender_name, sender_email = parseaddr(raw_from)
            sender_email = sender_email.strip().lower()
            if not sender_email or sender_email == cfg["user"].strip().lower():
                continue
            # Ignore automated mailer-daemon / noreply notifications unless useful
            subject = _decode_mime_header(msg.get("Subject")) or "(no subject)"
            message_id = (msg.get("Message-ID") or "").strip()
            references = (msg.get("References") or "").strip()
            body_text = _extract_email_text(msg)
            replies.append(
                {
                    "uid": str(uid),
                    "from_name": sender_name or sender_email,
                    "from_email": sender_email,
                    "subject": subject,
                    "message_id": message_id,
                    "references": f"{references} {message_id}".strip(),
                    "body": body_text[:2500],
                }
            )
        return max_uid, replies


def send_reply_email(
    to_email: str,
    subject: str,
    reply_text: str,
    in_reply_to: str = "",
    references: str = "",
    account_id: str | None = None,
) -> dict:
    """Send a threaded reply via the active Resend or SMTP sender account."""
    env = get_env()
    account = find_account(account_id, env)
    clean_subj = subject.strip()
    if not clean_subj.lower().startswith("re:"):
        clean_subj = f"Re: {clean_subj}"
    headers: dict[str, str] = {}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if references or in_reply_to:
        headers["References"] = (references or in_reply_to).strip()

    return send_via_account(
        account=account,
        to_email=to_email,
        subject=clean_subj,
        text_body=reply_text.strip(),
        html_body=render_html_from_text(reply_text.strip()),
        reply_headers=headers or None,
    )

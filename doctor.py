"""End-to-end mail, DNS (Cloudflare), Resend, IMAP, and Telegram diagnostics."""

from __future__ import annotations

import json
import sys

from core_service import csv_sources, get_env, get_mail_system_status
from mailer import get_sent_stats, load_sent_log
from tg_bot import fetch_bot_info, get_cached_tg_summary


def main() -> int:
    """Run live checks across .env, Resend, DNS (MX/SPF/DMARC), IMAP, Telegram, and leads."""
    env = get_env()
    health = get_mail_system_status(check_network=True)
    tg_token = env.get("TG_BOT_TOKEN", "").strip()
    tg_mode = env.get("TG_BOT_MODE", "auto").strip() or "auto"
    tg_info = fetch_bot_info(tg_token, timeout=6) if tg_token else {"ok": False, "error": "TG_BOT_TOKEN empty"}
    tg_cached = get_cached_tg_summary()
    sources = csv_sources()
    stats = get_sent_stats(load_sent_log())

    report = {
        "outbound": {
            "ready": health["outbound_ok"],
            "sender": f"{health['sender_name']} <{health['sender_email']}>".strip(),
            "reply_to": health["reply_to_email"],
            "resend_configured": health["resend_configured"],
            "resend_domain_status": health["resend_status"],
            "resend_sending": health["resend_sending"],
            "resend_error": health["resend_error"],
            "extra_smtp_accounts": health["smtp_count"],
        },
        "dns_cloudflare": {
            "domain": health["domain"],
            "mx_hosts": health["mx_hosts"],
            "cloudflare_routing_active": health["cloudflare_mx"],
            "spf_ok": health["spf_ok"],
            "dmarc_ok": health["dmarc_ok"],
            "dmarc_value": health["dmarc_value"],
            "dmarc_recommended": health["dmarc_recommended"],
            "dns_error": health["dns_error"],
        },
        "inbound_imap": {
            "configured": health["imap_configured"],
            "user": health["imap_user"],
        },
        "telegram": {
            "mode": tg_mode,
            "connected": bool(tg_info.get("ok")),
            "username": tg_info.get("username") or tg_cached.get("bot_username") or "",
            "authorized_chats": tg_cached.get("authorized_chats", []),
            "error": "" if tg_info.get("ok") else tg_info.get("error", ""),
        },
        "leads_and_stats": {
            "sources": sources,
            "sent_today": stats["sent_today"],
            "total_sent": stats["total_sent"],
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if health["outbound_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

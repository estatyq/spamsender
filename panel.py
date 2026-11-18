"""Local web dashboard server + embedded Telegram bot launcher."""

from __future__ import annotations

import csv
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import sys
import threading
from urllib.parse import parse_qs, urlparse
import urllib.request
import webbrowser

from core_service import (
    STATE,
    all_accounts,
    brand_vars,
    csv_sources,
    find_account,
    get_env,
    get_mail_system_status,
    import_leads_text,
    load_imap_config,
    load_inbox_store,
    mask_account,
    preview_template_safety,
    reply_to_inbox_item,
    save_imap_config,
    start_campaign_async,
    sync_imap_inbox,
    update_env_vars,
    update_inbox_status,
)
from guardrails import (
    HARD_DAILY_LIMIT_RESEND,
    HARD_DAILY_LIMIT_SMTP,
    MIN_DELAY_FLOOR_SEC,
    check_message_safety,
    load_blacklist,
    load_crm_contacts,
    save_blacklist,
    validate_template_syntax,
)
from mailer import (
    BASE_DIR,
    filter_queue,
    get_sent_stats,
    load_sent_log,
    load_smtp_accounts,
    load_templates_config,
    render_message,
    resolve_source_path,
    save_smtp_accounts,
    save_templates_config,
    select_template,
    send_via_account,
    test_smtp_connection,
)
from tg_bot import (
    TelegramBotRunner,
    fetch_bot_info,
    get_cached_tg_summary,
    send_bot_ping,
    start_bot_in_background,
)

PANEL_HTML_PATH = BASE_DIR / "panel.html"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PANEL_PORT", "8787"))

BOT_CTRL_LOCK = threading.Lock()
BOT_RUNTIME: dict[str, object] = {
    "runner": None,
    "configured_mode": "auto",
    "conflict_yielded": False,
    "bot_info": {},
}


def resolve_configured_tg_mode(env: dict[str, str], argv: list[str] | None = None) -> str:
    """Resolve Telegram bot polling mode from CLI flags and .env."""
    args = argv or []
    token = env.get("TG_BOT_TOKEN", "").strip()
    if not token:
        return "off"
    if "--no-bot" in args:
        return "remote"
    if "--bot" in args:
        return "local"
    raw = env.get("TG_BOT_MODE", "auto").strip().lower()
    if raw in ("remote", "server", "ui", "panel", "0", "false", "no"):
        return "remote"
    if raw in ("local", "poll", "1", "true", "yes"):
        return "local"
    if raw in ("off", "disabled", "none"):
        return "off"
    return "auto"


def _remote_panel_alive(env: dict[str, str]) -> bool:
    """Check optional REMOTE_PANEL_URL health before starting local bot polling."""
    remote_url = env.get("REMOTE_PANEL_URL", "").strip().rstrip("/")
    if not remote_url:
        return False
    try:
        with urllib.request.urlopen(f"{remote_url}/api/state", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _on_bot_conflict() -> None:
    """Yield local bot polling automatically if the server bot is active (409 Conflict)."""
    with BOT_CTRL_LOCK:
        BOT_RUNTIME["conflict_yielded"] = True
        BOT_RUNTIME["runner"] = None
        BOT_RUNTIME["configured_mode"] = "remote"
    STATE.append_log("Telegram 409 Conflict: server bot is active — switched local app to remote/connect mode.")


def sync_bot_runner(mode: str) -> None:
    """Start or stop background Telegram polling according to resolved mode."""
    env = get_env()
    token = env.get("TG_BOT_TOKEN", "").strip()
    if mode == "auto" and token and _remote_panel_alive(env):
        mode = "remote"

    with BOT_CTRL_LOCK:
        BOT_RUNTIME["configured_mode"] = mode
        current: TelegramBotRunner | None = BOT_RUNTIME.get("runner")  # type: ignore[assignment]
        if mode in ("remote", "off") or not token:
            if current is not None:
                current.stop_event.set()
            BOT_RUNTIME["runner"] = None
            return

        if current is not None and not current.stop_event.is_set():
            return

        BOT_RUNTIME["conflict_yielded"] = False
        BOT_RUNTIME["runner"] = start_bot_in_background(
            yield_on_conflict=True,
            on_conflict=_on_bot_conflict,
        )


def refresh_bot_info_async() -> None:
    """Refresh Telegram @username cleanly via getMe without polling getUpdates."""
    token = get_env().get("TG_BOT_TOKEN", "").strip()
    if not token:
        return

    def _worker() -> None:
        info = fetch_bot_info(token, timeout=5)
        with BOT_CTRL_LOCK:
            BOT_RUNTIME["bot_info"] = info

    threading.Thread(target=_worker, daemon=True).start()


def refresh_mail_health_async() -> None:
    """Refresh Resend + Cloudflare DNS (MX/SPF/DMARC) readiness in background."""
    def _worker() -> None:
        health = get_mail_system_status(check_network=True)
        with BOT_CTRL_LOCK:
            BOT_RUNTIME["mail_health"] = health

    threading.Thread(target=_worker, daemon=True).start()


def build_mail_health_state() -> dict[str, object]:
    """Merge cached DNS/Resend probe with live local IMAP and SMTP account counts."""
    live = get_mail_system_status(check_network=False)
    with BOT_CTRL_LOCK:
        cached = dict(BOT_RUNTIME.get("mail_health") or {})  # type: ignore[arg-type]
    if cached:
        merged = {**cached}
        merged["imap_configured"] = live["imap_configured"]
        merged["imap_user"] = live["imap_user"]
        merged["smtp_count"] = live["smtp_count"]
        merged["resend_configured"] = live["resend_configured"]
        merged["outbound_ok"] = bool(
            (live["resend_configured"] and cached.get("resend_status") == "verified")
            or int(live["smtp_count"]) > 0
            or (live["resend_configured"] and not cached.get("resend_error"))
        )
        return merged
    return live


def build_telegram_state(env: dict[str, str]) -> dict[str, object]:
    """Assemble safe Telegram status for web dashboard."""
    token = env.get("TG_BOT_TOKEN", "").strip()
    cached = get_cached_tg_summary()
    with BOT_CTRL_LOCK:
        runner: TelegramBotRunner | None = BOT_RUNTIME.get("runner")  # type: ignore[assignment]
        conf_mode = str(BOT_RUNTIME.get("configured_mode") or "auto")
        conflict = bool(BOT_RUNTIME.get("conflict_yielded"))
        bot_info = dict(BOT_RUNTIME.get("bot_info") or {})

    polling_active = bool(runner is not None and not runner.stop_event.is_set())
    if not token or conf_mode == "off":
        eff_mode = "off"
    elif polling_active:
        eff_mode = "local_polling"
    else:
        eff_mode = "remote_connect"

    username = str(bot_info.get("username") or cached.get("bot_username") or "")
    return {
        "configured": bool(token),
        "configured_mode": conf_mode,
        "effective_mode": eff_mode,
        "local_polling": polling_active,
        "conflict_yielded": conflict,
        "username": username,
        "bot_url": f"https://t.me/{username}" if username else "",
        "connected": bot_info.get("ok"),
        "last_error": str(bot_info.get("error") or ""),
        "authorized_chats": cached.get("authorized_chats", []),
        "notify_chat_id": env.get("TG_NOTIFY_CHAT_ID", ""),
        "proxy_configured": bool(env.get("TG_PROXY", "").strip()),
    }


class PanelHandler(BaseHTTPRequestHandler):
    """Serve static panel UI and JSON API endpoints."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send_json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 5_000_000:
            raise ValueError("Invalid request body size")
        data = self.rfile.read(length).decode("utf-8")
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            if not PANEL_HTML_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "panel.html missing"})
                return
            body = PANEL_HTML_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/state":
            env = get_env()
            accounts = [mask_account(a) for a in all_accounts(env)]
            sent_log = load_sent_log()
            stats = get_sent_stats(sent_log)
            imap_cfg = load_imap_config()
            inbox_items = load_inbox_store().get("items", [])
            inbox_pending = sum(1 for it in inbox_items if it.get("status") == "REPLIED")
            self._send_json(
                HTTPStatus.OK,
                {
                    "accounts": accounts,
                    "sources": csv_sources(),
                    "stats": stats,
                    "inbox_pending": inbox_pending,
                    "inbox_total": len(inbox_items),
                    "tg_enabled": bool(env.get("TG_BOT_TOKEN", "").strip()),
                    "telegram": build_telegram_state(env),
                    "mail_health": build_mail_health_state(),
                    "imap": {
                        "host": imap_cfg["host"],
                        "port": imap_cfg["port"],
                        "user": imap_cfg["user"],
                        "configured": bool(imap_cfg["user"] and imap_cfg["password"]),
                    },
                    "limits": {
                        "min_delay_floor": MIN_DELAY_FLOOR_SEC,
                        "hard_daily_resend": HARD_DAILY_LIMIT_RESEND,
                        "hard_daily_smtp": HARD_DAILY_LIMIT_SMTP,
                        "default_daily_limit": int(env.get("DAILY_LIMIT", "35")),
                        "default_min_delay": int(env.get("MIN_DELAY_SEC", "90")),
                        "default_max_delay": int(env.get("MAX_DELAY_SEC", "180")),
                    },
                    "templates": load_templates_config(),
                    "blacklist": sorted(load_blacklist()),
                    "campaign": STATE.snapshot(),
                    "recent_sent": [item for item in reversed(sent_log[-25:])],
                },
            )
            return

        if parsed.path == "/api/leads":
            params = parse_qs(parsed.query)
            source_name = (params.get("source", [""])[0] or "").strip() or None
            market = (params.get("market", ["ALL"])[0] or "ALL").upper()
            raw_site = (params.get("site_status", ["ALL"])[0] or "ALL").lower()
            site_status = "ALL" if raw_site in ("", "all") else raw_site
            query = (params.get("q", [""])[0] or "").strip().lower()

            env = get_env()
            bvars = brand_vars(env, env.get("SENDER_NAME", ""), env.get("SENDER_EMAIL", ""))
            source_path = resolve_source_path(source_name)
            if not source_path.exists():
                self._send_json(HTTPStatus.OK, {"rows": [], "stats": {"queued": 0}})
                return

            queue, stats = filter_queue(
                source_path=source_path,
                brand_vars=bvars,
                market_filter=market,
                site_filter=site_status,
            )
            if query:
                queue = [
                    r for r in queue
                    if query in r["email"].lower()
                    or query in r["company"].lower()
                    or query in r.get("city", "").lower()
                    or query in r.get("niche", "").lower()
                ]
            self._send_json(
                HTTPStatus.OK,
                {
                    "source": source_path.name,
                    "stats": stats,
                    "rows": queue[:100],
                },
            )
            return

        if parsed.path == "/api/inbox":
            imap_cfg = load_imap_config()
            store = load_inbox_store()
            sent_log = load_sent_log()
            self._send_json(
                HTTPStatus.OK,
                {
                    "imap_configured": bool(imap_cfg.get("user") and imap_cfg.get("password")),
                    "imap_user": imap_cfg.get("user", ""),
                    "items": list(reversed(store.get("items", []))),
                    "crm": list(
                        sorted(
                            load_crm_contacts().values(),
                            key=lambda x: x.get("updated_at", ""),
                            reverse=True,
                        )
                    ),
                    "sent": [item for item in reversed(sent_log[-250:])],
                },
            )
            return

        if parsed.path == "/api/sent/export":
            sent_log = load_sent_log()
            buf = io.StringIO()
            fields = ["sent_at", "status", "email", "company", "market", "template", "provider", "from", "subject", "id", "error"]
            writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in sent_log:
                writer.writerow({k: row.get(k, "") for k in fields})
            raw_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="sent_history.csv"')
            self.send_header("Content-Length", str(len(raw_bytes)))
            self.end_headers()
            self.wfile.write(raw_bytes)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        try:
            if parsed.path == "/api/safety/check":
                res = preview_template_safety(
                    market=str(body.get("market") or "EN").upper(),
                    site_status=str(body.get("site_status") or "no_site").lower(),
                    template_id=(body.get("template_id") or "").strip() or None,
                    custom_subject=body.get("subject"),
                    custom_body=body.get("body"),
                )
                self._send_json(HTTPStatus.OK, res)
            elif parsed.path == "/api/templates/save":
                self._handle_template_save(body)
            elif parsed.path == "/api/accounts/smtp":
                self._handle_smtp_add(body)
            elif parsed.path == "/api/accounts/delete":
                accounts = [a for a in load_smtp_accounts() if a.get("id") != str(body.get("id") or "").strip()]
                save_smtp_accounts(accounts)
                self._send_json(HTTPStatus.OK, {"ok": True})
            elif parsed.path == "/api/imap":
                save_imap_config(
                    host=str(body.get("host") or "imap.gmail.com"),
                    port=int(body.get("port") or 993),
                    user=str(body.get("user") or ""),
                    password=str(body.get("password") or ""),
                )
                self._send_json(HTTPStatus.OK, {"ok": True})
            elif parsed.path == "/api/blacklist":
                self._handle_blacklist(body)
            elif parsed.path == "/api/leads/import":
                res = import_leads_text(
                    raw_text=str(body.get("raw_text") or ""),
                    target_name=str(body.get("target") or "contacts.csv"),
                    market=str(body.get("market") or "EN").upper(),
                    site_status=str(body.get("site_status") or "no_site").lower(),
                )
                self._send_json(HTTPStatus.OK, res)
            elif parsed.path == "/api/test":
                self._handle_test_send(body)
            elif parsed.path == "/api/campaign/start":
                raw_site = str(body.get("site_status") or "ALL").lower()
                res = start_campaign_async(
                    account_id=str(body.get("account_id") or "resend_env"),
                    source_name=(body.get("source") or "").strip() or None,
                    market=str(body.get("market") or "ALL").upper(),
                    site_status="ALL" if raw_site in ("", "all") else raw_site,
                    template_id=(body.get("template_id") or "").strip() or None,
                    daily_limit=int(body.get("limit")) if body.get("limit") is not None else None,
                    min_delay=int(body.get("min_delay")) if body.get("min_delay") is not None else None,
                    max_delay=int(body.get("max_delay")) if body.get("max_delay") is not None else None,
                )
                self._send_json(HTTPStatus.OK, res)
            elif parsed.path == "/api/campaign/stop":
                STATE.stop_event.set()
                STATE.append_log("Получена команда остановки...")
                self._send_json(HTTPStatus.OK, {"ok": True})
            elif parsed.path == "/api/inbox/sync":
                added = sync_imap_inbox()
                self._send_json(HTTPStatus.OK, {"ok": True, "added": len(added)})
            elif parsed.path == "/api/inbox/reply":
                res = reply_to_inbox_item(
                    item_id=str(body.get("id") or ""),
                    reply_text=str(body.get("text") or ""),
                    via="web",
                    account_id=(body.get("account_id") or "").strip() or None,
                )
                self._send_json(HTTPStatus.OK, res)
            elif parsed.path == "/api/inbox/status":
                res = update_inbox_status(
                    item_id=str(body.get("id") or ""),
                    status=str(body.get("status") or ""),
                )
                self._send_json(HTTPStatus.OK, res)
            elif parsed.path == "/api/tg/mode":
                self._handle_tg_mode(body)
            elif parsed.path == "/api/tg/check":
                self._handle_tg_check(body)
            elif parsed.path == "/api/tg/ping":
                self._handle_tg_ping(body)
            elif parsed.path == "/api/mail/check":
                health = get_mail_system_status(check_network=True)
                with BOT_CTRL_LOCK:
                    BOT_RUNTIME["mail_health"] = health
                self._send_json(HTTPStatus.OK, {"ok": True, "mail_health": health})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _handle_template_save(self, body: dict) -> None:
        tpl_id = (body.get("template_id") or "").strip()
        subj = str(body.get("subject") or "").strip()
        text = str(body.get("body") or "").strip()
        if not tpl_id:
            raise ValueError("Missing template_id.")
        errs = validate_template_syntax(subj) + validate_template_syntax(text)
        if errs:
            raise ValueError("Template syntax error: " + "; ".join(errs))

        cfg = load_templates_config()
        updated = False
        for group_name in ("by_key", "extra"):
            for item in cfg.get(group_name, {}).values():
                if item.get("id") == tpl_id:
                    item["subject"] = subj
                    item["body"] = text
                    updated = True
        if not updated:
            raise ValueError(f"Template '{tpl_id}' not found.")
        save_templates_config(cfg)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_smtp_add(self, body: dict) -> None:
        host = str(body.get("smtp_host") or "").strip()
        port = int(body.get("smtp_port") or 465)
        user = str(body.get("smtp_user") or "").strip()
        password = str(body.get("smtp_password") or "").strip()
        sender_name = str(body.get("sender_name") or "").strip()
        sender_email = str(body.get("sender_email") or user).strip()
        reply_to = str(body.get("reply_to_email") or sender_email).strip()

        if not host or not user or not password or not sender_email:
            raise ValueError("Fill in SMTP server, user, password, and sender email.")

        test_smtp_connection(host, port, user, password)
        accounts = load_smtp_accounts()
        acc_id = f"smtp_{user.lower().replace('@', '_at_')}"
        accounts = [a for a in accounts if a.get("id") != acc_id]
        accounts.append(
            {
                "id": acc_id,
                "label": f"SMTP ({sender_email})",
                "provider": "smtp",
                "smtp_host": host,
                "smtp_port": port,
                "smtp_user": user,
                "smtp_password": password,
                "sender_name": sender_name or sender_email.split("@")[0],
                "sender_email": sender_email,
                "reply_to_email": reply_to,
            }
        )
        save_smtp_accounts(accounts)
        self._send_json(HTTPStatus.OK, {"ok": True, "id": acc_id})

    def _handle_blacklist(self, body: dict) -> None:
        action = body.get("action", "add")
        entry = str(body.get("entry") or "").strip().lower()
        if not entry:
            raise ValueError("Enter email or @domain.")
        current = load_blacklist()
        if action == "remove":
            current.discard(entry)
        else:
            current.add(entry)
        saved = save_blacklist(list(current))
        self._send_json(HTTPStatus.OK, {"ok": True, "blacklist": saved})

    def _handle_test_send(self, body: dict) -> None:
        env = get_env()
        account = find_account(str(body.get("account_id") or "resend_env"), env)
        target_email = str(body.get("target_email") or "").strip()
        if not target_email or "@" not in target_email:
            raise ValueError("Enter a valid target email.")

        bvars = brand_vars(env, account["sender_name"], account["sender_email"])
        templates_cfg = load_templates_config()
        chosen_id, subj_tpl, body_tpl = select_template(
            templates_cfg,
            market=str(body.get("market") or "EN").upper(),
            site_status=str(body.get("site_status") or "no_site").lower(),
            template_id=(body.get("template_id") or "").strip() or None,
        )
        subj, text_body, html_body = render_message(
            subj_tpl,
            body_tpl,
            {**bvars, "name": "Alex", "company": "Demo Auto Service", "email": target_email},
        )
        safety = check_message_safety(subj_tpl, body_tpl, subj, text_body)
        if not safety["ok"]:
            raise ValueError("; ".join(safety["errors"]))  # type: ignore[arg-type]

        res = send_via_account(account, target_email, subj, text_body, html_body)
        self._send_json(HTTPStatus.OK, {"ok": True, "id": res.get("id"), "template_id": chosen_id})

    def _handle_tg_mode(self, body: dict) -> None:
        mode = str(body.get("mode") or "remote").strip().lower()
        if mode not in ("remote", "auto", "local", "off"):
            raise ValueError("Invalid TG bot mode.")
        updates: dict[str, str] = {"TG_BOT_MODE": mode}
        if "notify_chat_id" in body:
            updates["TG_NOTIFY_CHAT_ID"] = str(body.get("notify_chat_id") or "").strip()
        if "proxy" in body:
            proxy_val = str(body.get("proxy") or "").strip()
            if proxy_val or body.get("clear_proxy"):
                updates["TG_PROXY"] = proxy_val
        env = update_env_vars(updates)
        resolved = resolve_configured_tg_mode(env, [])
        sync_bot_runner(resolved)
        self._send_json(HTTPStatus.OK, {"ok": True, "telegram": build_telegram_state(env)})

    def _handle_tg_check(self, body: dict) -> None:
        env = get_env()
        token = env.get("TG_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TG_BOT_TOKEN is not configured in .env.")
        proxy = str(body.get("proxy") or "").strip() or None
        info = fetch_bot_info(token, proxy=proxy, timeout=6)
        with BOT_CTRL_LOCK:
            BOT_RUNTIME["bot_info"] = info
        if not info.get("ok"):
            raise ValueError(str(info.get("error") or "Telegram API unreachable."))
        self._send_json(HTTPStatus.OK, {"ok": True, "info": info, "telegram": build_telegram_state(env)})

    def _handle_tg_ping(self, body: dict) -> None:
        env = get_env()
        token = env.get("TG_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TG_BOT_TOKEN is not configured in .env.")
        proxy = str(body.get("proxy") or "").strip() or None
        chat_id = str(body.get("chat_id") or "").strip() or None
        stats = get_sent_stats(load_sent_log())
        text = str(body.get("text") or "").strip() or (
            f"🔗 Web Panel ↔ Telegram Bot connected!\n"
            f"Sent today: {stats['sent_today']} | Total sent: {stats['total_sent']}\n"
            f"Local polling: OFF (server bot mode)."
        )
        res = send_bot_ping(token, text, chat_id=chat_id, proxy=proxy, timeout=8)
        self._send_json(HTTPStatus.OK, res)


def main() -> int:
    """Start local web panel server and optional background Telegram bot."""
    env = get_env()
    mode = resolve_configured_tg_mode(env, sys.argv)
    sync_bot_runner(mode)
    refresh_bot_info_async()
    refresh_mail_health_async()

    server = ThreadingHTTPServer((HOST, PORT), PanelHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"Email Outreach Panel running at {url}")
    with BOT_CTRL_LOCK:
        active_runner = BOT_RUNTIME.get("runner")
    if active_runner:
        print("Telegram bot thread active (local polling).")
    elif mode == "remote":
        print("Telegram bot in REMOTE mode (local polling off; server handles bot).")
    else:
        print("Telegram bot disabled (no TG_BOT_TOKEN or mode=off).")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down panel...")
    finally:
        STATE.stop_event.set()
        with BOT_CTRL_LOCK:
            runner: TelegramBotRunner | None = BOT_RUNTIME.get("runner")  # type: ignore[assignment]
            if runner:
                runner.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Local web dashboard server + embedded Telegram bot launcher."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser

from core_service import (
    STATE,
    all_accounts,
    brand_vars,
    csv_sources,
    find_account,
    get_env,
    import_leads_text,
    load_imap_config,
    mask_account,
    preview_template_safety,
    save_imap_config,
    start_campaign_async,
)
from guardrails import (
    HARD_DAILY_LIMIT_RESEND,
    HARD_DAILY_LIMIT_SMTP,
    MIN_DELAY_FLOOR_SEC,
    check_message_safety,
    load_blacklist,
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
from tg_bot import start_bot_in_background

PANEL_HTML_PATH = BASE_DIR / "panel.html"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PANEL_PORT", "8787"))


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
            self._send_json(
                HTTPStatus.OK,
                {
                    "accounts": accounts,
                    "sources": csv_sources(),
                    "stats": stats,
                    "tg_enabled": bool(env.get("TG_BOT_TOKEN", "").strip()),
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


def main() -> int:
    """Start local web panel server and background Telegram bot (if configured)."""
    bot_runner = start_bot_in_background()
    server = ThreadingHTTPServer((HOST, PORT), PanelHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"Email Outreach Panel running at {url}")
    if bot_runner:
        print("Telegram bot thread active.")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in os.sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down panel...")
    finally:
        STATE.stop_event.set()
        if bot_runner:
            bot_runner.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

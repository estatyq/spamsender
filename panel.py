"""Local web dashboard server for Email Outreach Sender (zero external deps)."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser

from guardrails import (
    HARD_DAILY_LIMIT_RESEND,
    HARD_DAILY_LIMIT_SMTP,
    MIN_DELAY_FLOOR_SEC,
    check_message_safety,
    enforce_safe_pacing,
    load_blacklist,
    save_blacklist,
    validate_template_syntax,
)
from mailer import (
    BASE_DIR,
    CONTACTS_PATH,
    ENV_PATH,
    LEADS_READY_PATH,
    build_default_resend_account,
    execute_campaign,
    filter_queue,
    get_sent_stats,
    load_env,
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
from prepare_leads import check_domain_mx, is_clean_syntax

PANEL_HTML_PATH = BASE_DIR / "panel.html"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PANEL_PORT", "8787"))


class CampaignState:
    """Thread-safe holder for active campaign execution and live logs."""

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


def _brand_vars(env: dict[str, str], sender_name: str, sender_email: str) -> dict[str, str]:
    return {
        "brand_name": (env.get("BRAND_NAME") or sender_name or "Our Team").strip(),
        "brand_url": (
            env.get("BRAND_URL")
            or (sender_email.split("@")[-1] if "@" in sender_email else "yourdomain.com")
        ).strip(),
    }


def _all_accounts(env: dict[str, str]) -> list[dict]:
    accounts: list[dict] = []
    resend_acc = build_default_resend_account(env)
    if resend_acc["api_key"]:
        accounts.append(resend_acc)
    accounts.extend(load_smtp_accounts())
    return accounts


def _mask_account(acc: dict) -> dict:
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


def _find_account(account_id: str, env: dict[str, str]) -> dict:
    for acc in _all_accounts(env):
        if acc.get("id") == account_id:
            return acc
    raise ValueError(f"Аккаунт '{account_id}' не найден.")


def _csv_sources() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for path in (LEADS_READY_PATH, CONTACTS_PATH):
        if path.exists():
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                count = sum(1 for _ in csv.DictReader(fh))
            items.append({"name": path.name, "count": count})
    return items


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
            env = {**load_env(ENV_PATH), **os.environ}
            accounts = [_mask_account(a) for a in _all_accounts(env)]
            sent_log = load_sent_log()
            stats = get_sent_stats(sent_log)
            templates_cfg = load_templates_config()
            blacklist = sorted(load_blacklist())
            self._send_json(
                HTTPStatus.OK,
                {
                    "accounts": accounts,
                    "sources": _csv_sources(),
                    "stats": stats,
                    "limits": {
                        "min_delay_floor": MIN_DELAY_FLOOR_SEC,
                        "hard_daily_resend": HARD_DAILY_LIMIT_RESEND,
                        "hard_daily_smtp": HARD_DAILY_LIMIT_SMTP,
                        "default_daily_limit": int(env.get("DAILY_LIMIT", "35")),
                        "default_min_delay": int(env.get("MIN_DELAY_SEC", "90")),
                        "default_max_delay": int(env.get("MAX_DELAY_SEC", "180")),
                    },
                    "templates": templates_cfg,
                    "blacklist": blacklist,
                    "campaign": STATE.snapshot(),
                    "recent_sent": [
                        item for item in reversed(sent_log[-25:])
                    ],
                },
            )
            return

        if parsed.path == "/api/leads":
            params = parse_qs(parsed.query)
            source_name = (params.get("source", [""])[0] or "").strip() or None
            market = (params.get("market", ["ALL"])[0] or "ALL").upper()
            site_status = (params.get("site_status", ["ALL"])[0] or "ALL").lower()
            query = (params.get("q", [""])[0] or "").strip().lower()

            env = {**load_env(ENV_PATH), **os.environ}
            bvars = _brand_vars(env, env.get("SENDER_NAME", ""), env.get("SENDER_EMAIL", ""))
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
                self._handle_safety_check(body)
            elif parsed.path == "/api/templates/save":
                self._handle_template_save(body)
            elif parsed.path == "/api/accounts/smtp":
                self._handle_smtp_add(body)
            elif parsed.path == "/api/accounts/delete":
                self._handle_smtp_delete(body)
            elif parsed.path == "/api/blacklist":
                self._handle_blacklist(body)
            elif parsed.path == "/api/leads/import":
                self._handle_leads_import(body)
            elif parsed.path == "/api/test":
                self._handle_test_send(body)
            elif parsed.path == "/api/campaign/start":
                self._handle_campaign_start(body)
            elif parsed.path == "/api/campaign/stop":
                STATE.stop_event.set()
                STATE.append_log("Получена команда остановки...")
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _handle_safety_check(self, body: dict) -> None:
        env = {**load_env(ENV_PATH), **os.environ}
        bvars = _brand_vars(env, env.get("SENDER_NAME", ""), env.get("SENDER_EMAIL", ""))
        templates_cfg = load_templates_config()
        tpl_id = (body.get("template_id") or "").strip() or None
        market = (body.get("market") or "EN").upper()
        site_status = (body.get("site_status") or "no_site").lower()

        custom_subject = body.get("subject")
        custom_body = body.get("body")
        if custom_subject is not None and custom_body is not None:
            chosen_id = tpl_id or "custom"
            subj_tpl, body_tpl = str(custom_subject), str(custom_body)
        else:
            chosen_id, subj_tpl, body_tpl = select_template(
                templates_cfg,
                market=market if market != "ALL" else "EN",
                site_status=site_status if site_status != "ALL" else "no_site",
                template_id=tpl_id,
            )

        sample_vars = {
            **bvars,
            "name": "Alex",
            "company": "Demo Auto Service",
            "email": "client@example.org",
        }
        rendered_subj, rendered_body, _ = render_message(subj_tpl, body_tpl, sample_vars)
        safety = check_message_safety(subj_tpl, body_tpl, rendered_subj, rendered_body)
        self._send_json(
            HTTPStatus.OK,
            {
                "template_id": chosen_id,
                "rendered_subject": rendered_subj,
                "rendered_body": rendered_body,
                "safety": safety,
            },
        )

    def _handle_template_save(self, body: dict) -> None:
        tpl_id = (body.get("template_id") or "").strip()
        subj = str(body.get("subject") or "").strip()
        text = str(body.get("body") or "").strip()
        if not tpl_id:
            raise ValueError("Укажите template_id.")
        errs = validate_template_syntax(subj) + validate_template_syntax(text)
        if errs:
            raise ValueError("Ошибка синтаксиса шаблона: " + "; ".join(errs))

        cfg = load_templates_config()
        updated = False
        for group_name in ("by_key", "extra"):
            group = cfg.get(group_name, {})
            for item in group.values():
                if item.get("id") == tpl_id:
                    item["subject"] = subj
                    item["body"] = text
                    updated = True
        if not updated:
            raise ValueError(f"Шаблон '{tpl_id}' не найден.")
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
            raise ValueError("Заполните SMTP сервер, логин, пароль и email отправителя.")

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

    def _handle_smtp_delete(self, body: dict) -> None:
        acc_id = str(body.get("id") or "").strip()
        accounts = [a for a in load_smtp_accounts() if a.get("id") != acc_id]
        save_smtp_accounts(accounts)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_blacklist(self, body: dict) -> None:
        action = body.get("action", "add")
        entry = str(body.get("entry") or "").strip().lower()
        if not entry:
            raise ValueError("Введите email или @домен.")
        current = load_blacklist()
        if action == "remove":
            current.discard(entry)
        else:
            current.add(entry)
        saved = save_blacklist(list(current))
        self._send_json(HTTPStatus.OK, {"ok": True, "blacklist": saved})

    def _handle_leads_import(self, body: dict) -> None:
        raw_text = str(body.get("raw_text") or "").strip()
        market = (body.get("market") or "EN").strip().upper()
        site_status = (body.get("site_status") or "no_site").strip().lower()
        target_name = (body.get("target") or "contacts.csv").strip()
        target_path = CONTACTS_PATH if target_name == "contacts.csv" else LEADS_READY_PATH

        if not raw_text:
            raise ValueError("Вставьте список email или CSV-строки.")

        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        parsed_rows: list[dict[str, str]] = []
        if lines and ("email" in lines[0].lower() and "," in lines[0]):
            for row in csv.DictReader(lines):
                email = (row.get("email") or row.get("Email") or "").strip().lower()
                if email:
                    parsed_rows.append(
                        {
                            "email": email,
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
                email = parts[0].lower()
                company = parts[1] if len(parts) > 1 and parts[1] else "your team"
                parsed_rows.append(
                    {
                        "email": email,
                        "company": company,
                        "market": market,
                        "country": "",
                        "city": "",
                        "niche": "",
                        "site_status": site_status,
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

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "target": target_path.name,
                "added": added,
                "duplicates": duplicates,
                "invalid_syntax": invalid_syntax,
                "invalid_mx": invalid_mx,
            },
        )

    def _handle_test_send(self, body: dict) -> None:
        env = {**load_env(ENV_PATH), **os.environ}
        account = _find_account(str(body.get("account_id") or "resend_env"), env)
        target_email = str(body.get("target_email") or "").strip()
        if not target_email or "@" not in target_email:
            raise ValueError("Введите корректный email для тестовой отправки.")

        bvars = _brand_vars(env, account["sender_name"], account["sender_email"])
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
            raise ValueError("Защита от спама заблокировала тест: " + "; ".join(safety["errors"]))  # type: ignore[arg-type]

        res = send_via_account(account, target_email, subj, text_body, html_body)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "id": res.get("id"), "template_id": chosen_id},
        )

    def _handle_campaign_start(self, body: dict) -> None:
        with STATE.lock:
            if STATE.running:
                raise ValueError("Рассылка уже запущена! Сначала дождитесь окончания или нажмите СТОП.")
            STATE.running = True
            STATE.stop_event.clear()
            STATE.progress_current = 0
            STATE.progress_total = 0
            STATE.last_result = None
            STATE.logs = []

        env = {**load_env(ENV_PATH), **os.environ}
        try:
            account = _find_account(str(body.get("account_id") or "resend_env"), env)
            bvars = _brand_vars(env, account["sender_name"], account["sender_email"])
            source_path = resolve_source_path((body.get("source") or "").strip() or None)
            market = str(body.get("market") or "ALL").upper()
            site_status = str(body.get("site_status") or "ALL").lower()
            tpl_id = (body.get("template_id") or "").strip() or None
            daily_limit = int(body.get("limit") or env.get("DAILY_LIMIT", "35"))
            min_delay = int(body.get("min_delay") or env.get("MIN_DELAY_SEC", "90"))
            max_delay = int(body.get("max_delay") or env.get("MAX_DELAY_SEC", "180"))
            safe_limit, safe_min, safe_max, _ = enforce_safe_pacing(
                account.get("provider", "resend"), daily_limit, min_delay, max_delay
            )
        except Exception:
            with STATE.lock:
                STATE.running = False
            raise

        def _worker() -> None:
            try:
                res = execute_campaign(
                    account=account,
                    brand_vars=bvars,
                    source_path=source_path,
                    market_filter=market,
                    site_filter=site_status,
                    template_id=tpl_id,
                    daily_limit=safe_limit,
                    min_delay=safe_min,
                    max_delay=safe_max,
                    dry_run=False,
                    stop_event=STATE.stop_event,
                    log_cb=STATE.append_log,
                    progress_cb=STATE.set_progress,
                )
                with STATE.lock:
                    STATE.last_result = res
            except Exception as exc:  # noqa: BLE001
                STATE.append_log(f"[ОШИБКА] {exc}")
                with STATE.lock:
                    STATE.last_result = {"ok": False, "error": str(exc)}
            finally:
                with STATE.lock:
                    STATE.running = False

        threading.Thread(target=_worker, daemon=True).start()
        self._send_json(HTTPStatus.OK, {"ok": True})


def main() -> int:
    """Start local web panel server and open browser."""
    server = ThreadingHTTPServer((HOST, PORT), PanelHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"Email Outreach Panel running at {url}")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in os.sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down panel...")
    finally:
        STATE.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

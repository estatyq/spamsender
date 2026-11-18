"""Multi-language Telegram bot (EN / RU / ID) for outreach launch, lead MX import, and IMAP replies."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core_service import (
    STATE,
    brand_vars,
    fetch_new_imap_replies,
    find_account,
    get_env,
    import_leads_text,
    load_imap_config,
    preview_template_safety,
    send_reply_email,
    start_campaign_async,
)
from guardrails import (
    DATA_DIR,
    check_message_safety,
    load_blacklist,
    load_crm_contacts,
    save_blacklist,
    upsert_crm_contact,
)
from mailer import (
    BASE_DIR,
    LEADS_READY_PATH,
    CONTACTS_PATH,
    filter_queue,
    get_sent_stats,
    load_sent_log,
    load_templates_config,
    render_message,
    resolve_source_path,
    select_template,
    send_via_account,
)

TG_STATE_PATH = DATA_DIR / "tg_state.json"
STATE_FILE_LOCK = threading.Lock()

STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "choose_lang": "🌐 Выберите язык интерфейса бота:",
        "lang_saved": "Язык переключён на Русский.",
        "ask_pin": "🔐 Введите ПИН-код доступа (4 цифры), чтобы открыть бота:",
        "bad_pin": "❌ Неверный код. Попробуйте ещё раз:",
        "auth_ok": "✅ Доступ открыт!",
        "menu_title": "📬 Панель управления рассылкой\nКанал: {sender}\nСегодня отправлено: {today} | Всего: {total}\nСтатус: {status}",
        "status_running": "🟢 Идёт отправка ({cur}/{tot})",
        "status_idle": "⚪ Ожидание",
        "btn_start": "🚀 Запустить рассылку",
        "btn_status": "📊 Статус / Стоп",
        "btn_test": "🧪 Тест на email",
        "btn_import": "📥 Загрузить базу",
        "btn_blacklist": "🚫 Стоп-лист",
        "btn_inbox": "📩 Входящие ответы",
        "btn_lang": "🌐 Язык (RU/EN/ID)",
        "btn_stop": "🛑 Остановить рассылку",
        "btn_cancel": "❌ Отмена",
        "btn_confirm": "✅ Подтвердить и запустить",
        "btn_reply": "💬 Ответить",
        "btn_block_sender": "🚫 В стоп-лист",
        "btn_lead": "⭐ Лид (в работу)",
        "btn_done": "✅ Обработан",
        "lead_saved": "⭐ Контакт `{email}` зафиксирован как Лид (больше не попадёт в холодную рассылку).",
        "done_saved": "✅ Контакт `{email}` отмечен как Обработан.",
        "wiz_market": "Шаг 1/3 — Выберите рынок базы:",
        "wiz_site": "Шаг 2/3 — Фильтр по сайту компании:",
        "wiz_limit": "Шаг 3/3 — Сколько писем отправить в этой волне?",
        "site_no": "🔥 Без сайта",
        "site_has": "🌐 Есть сайт",
        "site_all": "Все",
        "mkt_all": "Все рынки",
        "wiz_confirm": "🔍 Проверка перед стартом:\n• Рынок: {market} | Сайт: {site}\n• В очереди подходит: {queued}\n• Будет отправлено до: {limit} писем (пауза {min_d}–{max_d}с)\n• От кого: {sender}\n• Анти-спам рейтинг: {score}/100\n\nПример темы: {subject}\n\n{preview}",
        "wiz_blocked": "⛔ Запуск заблокирован защитой от спама:\n{errors}",
        "wiz_empty": "⚠️ По выбранному фильтру нет неотправленных контактов.",
        "started": "🚀 Рассылка запущена! Остановить можно в любой момент кнопкой «Статус / Стоп».",
        "already_running": "⚠️ Рассылка уже идёт. Сначала остановите текущую.",
        "stopped": "🛑 Команда остановки отправлена.",
        "ask_test_email": "✉️ Введите email, на который отправить 1 тестовое письмо (или «Отмена»):",
        "test_sent": "✅ Тестовое письмо отправлено на {email} (ID: {id}).",
        "ask_import": "📥 Пришлите .csv/.txt файл с почтами ИЛИ отправьте список email текстом (по одному на строке, можно `email,Компания`).\nВсе домены автоматически пройдут MX-проверку.",
        "import_done": "✅ Импорт в {target} завершён:\n• Добавлено: {added}\n• Дублей пропущено: {dup}\n• Кривой формат: {syn}\n• Битый домен (нет MX): {mx}",
        "ask_blacklist": "🚫 Введите email или `@domain.com`, чтобы навсегда добавить в стоп-лист:",
        "blacklist_done": "✅ Добавлено в стоп-лист: `{entry}`",
        "inbox_not_cfg": "📩 Входящие ответы пока приходят на ваш Email (`{reply_to}`).\n\nЧтобы ответы клиентов приходили прямо сюда в Telegram и на них можно было отвечать из чата, укажите в веб-панели (вкладка 3 → IMAP) или в `.env` почту и 16-символьный пароль приложения Gmail.",
        "inbox_ready": "📩 Подключён ящик входящих: `{user}`.\nНовые ответы клиентов автоматически приходят сюда с кнопкой «💬 Ответить».",
        "new_reply": "📩 Новый ответ от клиента!\nОт: {name} <{email}>\nТема: {subject}\n\n{body}\n\n💡 Можете просто ответить свайпом на это сообщение или нажать «💬 Ответить».",
        "ask_reply_text": "✍️ Напишите текст ответа для `{email}` (тема: {subject}):",
        "reply_sent": "✅ Ответ отправлен клиенту `{email}` от имени `{sender}`!",
        "err_prefix": "❌ Ошибка: {err}",
    },
    "en": {
        "choose_lang": "🌐 Choose bot interface language:",
        "lang_saved": "Language switched to English.",
        "ask_pin": "🔐 Enter the 4-digit access PIN to unlock the bot:",
        "bad_pin": "❌ Wrong PIN. Please try again:",
        "auth_ok": "✅ Access granted!",
        "menu_title": "📬 Outreach Control Panel\nSender: {sender}\nSent today: {today} | Total: {total}\nStatus: {status}",
        "status_running": "🟢 Sending ({cur}/{tot})",
        "status_idle": "⚪ Idle",
        "btn_start": "🚀 Start Campaign",
        "btn_status": "📊 Status / Stop",
        "btn_test": "🧪 Send Test",
        "btn_import": "📥 Import Leads",
        "btn_blacklist": "🚫 Blacklist",
        "btn_inbox": "📩 Inbox Replies",
        "btn_lang": "🌐 Language (RU/EN/ID)",
        "btn_stop": "🛑 Stop Campaign",
        "btn_cancel": "❌ Cancel",
        "btn_confirm": "✅ Confirm & Start",
        "btn_reply": "💬 Reply",
        "btn_block_sender": "🚫 Blacklist",
        "btn_lead": "⭐ Mark Lead",
        "btn_done": "✅ Mark Done",
        "lead_saved": "⭐ Saved `{email}` as Lead (excluded from future cold blasts).",
        "done_saved": "✅ Marked `{email}` as Done.",
        "wiz_market": "Step 1/3 — Select target market:",
        "wiz_site": "Step 2/3 — Filter by company website:",
        "wiz_limit": "Step 3/3 — How many emails to send in this batch?",
        "site_no": "🔥 No Website",
        "site_has": "🌐 Has Website",
        "site_all": "All",
        "mkt_all": "All Markets",
        "wiz_confirm": "🔍 Preflight Check:\n• Market: {market} | Site: {site}\n• Eligible in queue: {queued}\n• Batch cap: up to {limit} emails (delay {min_d}–{max_d}s)\n• From: {sender}\n• Spam score: {score}/100\n\nSubject: {subject}\n\n{preview}",
        "wiz_blocked": "⛔ Blocked by anti-spam guard:\n{errors}",
        "wiz_empty": "⚠️ No unsent contacts match this filter.",
        "started": "🚀 Campaign started! Use «Status / Stop» anytime to abort.",
        "already_running": "⚠️ A campaign is already running.",
        "stopped": "🛑 Stop signal sent.",
        "ask_test_email": "✉️ Enter the email address for 1 test message:",
        "test_sent": "✅ Test email sent to {email} (ID: {id}).",
        "ask_import": "📥 Upload a .csv/.txt file OR paste emails line by line (`email` or `email,Company`).\nAll domains will be DNS-MX verified automatically.",
        "import_done": "✅ Imported into {target}:\n• Added: {added}\n• Duplicates skipped: {dup}\n• Invalid syntax: {syn}\n• Dead domain (no MX): {mx}",
        "ask_blacklist": "🚫 Enter an email or `@domain.com` to block permanently:",
        "blacklist_done": "✅ Added to blacklist: `{entry}`",
        "inbox_not_cfg": "📩 Replies currently arrive at `{reply_to}`.\n\nTo receive & reply to client emails directly inside Telegram, configure `IMAP_USER` and `IMAP_PASSWORD` (Gmail App Password) in the web panel or `.env`.",
        "inbox_ready": "📩 Inbox connected: `{user}`.\nNew client replies will appear here automatically with a «💬 Reply» button.",
        "new_reply": "📩 New client reply!\nFrom: {name} <{email}>\nSubject: {subject}\n\n{body}\n\n💡 Swipe-reply directly to this message or tap «💬 Reply».",
        "ask_reply_text": "✍️ Type your reply to `{email}` (Subject: {subject}):",
        "reply_sent": "✅ Reply sent to `{email}` as `{sender}`!",
        "err_prefix": "❌ Error: {err}",
    },
    "id": {
        "choose_lang": "🌐 Pilih bahasa antarmuka bot:",
        "lang_saved": "Bahasa diubah ke Bahasa Indonesia.",
        "ask_pin": "🔐 Masukkan kode PIN akses (4 digit) untuk membuka bot:",
        "bad_pin": "❌ Kode salah. Silakan coba lagi:",
        "auth_ok": "✅ Akses diterima!",
        "menu_title": "📬 Panel Kontrol Email Outreach\nPengirim: {sender}\nTerkirim hari ini: {today} | Total: {total}\nStatus: {status}",
        "status_running": "🟢 Sedang mengirim ({cur}/{tot})",
        "status_idle": "⚪ Siap",
        "btn_start": "🚀 Mulai Kampanye",
        "btn_status": "📊 Status / Stop",
        "btn_test": "🧪 Kirim Tes",
        "btn_import": "📥 Tambah Kontak",
        "btn_blacklist": "🚫 Daftar Hitam",
        "btn_inbox": "📩 Kotak Masuk",
        "btn_lang": "🌐 Bahasa (RU/EN/ID)",
        "btn_stop": "🛑 Hentikan Kampanye",
        "btn_cancel": "❌ Batal",
        "btn_confirm": "✅ Konfirmasi & Mulai",
        "btn_reply": "💬 Balas",
        "btn_block_sender": "🚫 Blokir",
        "btn_lead": "⭐ Tandai Prospek",
        "btn_done": "✅ Selesai",
        "lead_saved": "⭐ `{email}` ditandai sebagai Prospek (tidak akan dikirimi blast lagi).",
        "done_saved": "✅ `{email}` ditandai Selesai.",
        "wiz_market": "Langkah 1/3 — Pilih target pasar:",
        "wiz_site": "Langkah 2/3 — Filter status website:",
        "wiz_limit": "Langkah 3/3 — Jumlah maksimal email gelombang ini:",
        "site_no": "🔥 Belum Punya Website",
        "site_has": "🌐 Punya Website",
        "site_all": "Semua",
        "mkt_all": "Semua Pasar",
        "wiz_confirm": "🔍 Pemeriksaan Anti-Spam:\n• Pasar: {market} | Website: {site}\n• Antrean siap kirim: {queued}\n• Batas gelombang: {limit} email (jeda {min_d}–{max_d} detik)\n• Pengirim: {sender}\n• Skor spam: {score}/100\n\nSubjek: {subject}\n\n{preview}",
        "wiz_blocked": "⛔ Diblokir oleh proteksi anti-spam:\n{errors}",
        "wiz_empty": "⚠️ Tidak ada kontak yang cocok dengan filter ini.",
        "started": "🚀 Kampanye dimulai! Tekan «Status / Stop» kapan saja untuk berhenti.",
        "already_running": "⚠️ Kampanye sedang berjalan.",
        "stopped": "🛑 Sinyal berhenti dikirim.",
        "ask_test_email": "✉️ Masukkan alamat email untuk menerima 1 pesan tes:",
        "test_sent": "✅ Email tes terkirim ke {email} (ID: {id}).",
        "ask_import": "📥 Kirim file .csv/.txt ATAU tempel daftar email per baris (`email` atau `email,Nama Perusahaan`).\nSemua domain akan diperiksa lewat DNS MX secara otomatis.",
        "import_done": "✅ Selesai impor ke {target}:\n• Ditambahkan: {added}\n• Duplikat: {dup}\n• Format salah: {syn}\n• Domain mati (tanpa MX): {mx}",
        "ask_blacklist": "🚫 Masukkan email atau `@domain.com` untuk diblokir permanen:",
        "blacklist_done": "✅ Ditambahkan ke daftar hitam: `{entry}`",
        "inbox_not_cfg": "📩 Balasan klien saat ini masuk ke `{reply_to}`.\n\nAgar balasan masuk langsung ke Telegram dan bisa dibalas dari chat ini, isi `IMAP_USER` & `IMAP_PASSWORD` (App Password Gmail) di panel web atau `.env`.",
        "inbox_ready": "📩 Kotak masuk terhubung: `{user}`.\nBalasan baru akan muncul otomatis di sini dengan tombol «💬 Balas».",
        "new_reply": "📩 Balasan baru dari klien!\nDari: {name} <{email}>\nSubjek: {subject}\n\n{body}\n\n💡 Geser pesan ini untuk membalas langsung atau tekan «💬 Balas».",
        "ask_reply_text": "✍️ Ketik balasan untuk `{email}` (Subjek: {subject}):",
        "reply_sent": "✅ Balasan terkirim ke `{email}` atas nama `{sender}`!",
        "err_prefix": "❌ Error: {err}",
    },
}


def _load_state() -> dict:
    with STATE_FILE_LOCK:
        if not TG_STATE_PATH.exists():
            return {
                "authorized_chats": [],
                "chat_langs": {},
                "pending": {},
                "last_imap_uid": 0,
                "replies": {},
                "tg_msg_map": {},
                "next_reply_id": 1,
            }
        data = json.loads(TG_STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("authorized_chats", [])
        data.setdefault("chat_langs", {})
        data.setdefault("pending", {})
        data.setdefault("last_imap_uid", 0)
        data.setdefault("replies", {})
        data.setdefault("tg_msg_map", {})
        data.setdefault("next_reply_id", 1)
        return data


def _save_state(data: dict) -> None:
    with STATE_FILE_LOCK:
        TG_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _t(state: dict, chat_id: int, key: str, **kwargs: object) -> str:
    lang = state.get("chat_langs", {}).get(str(chat_id), "ru")
    bundle = STRINGS.get(lang, STRINGS["ru"])
    tpl = bundle.get(key, STRINGS["ru"].get(key, key))
    return tpl.format(**kwargs) if kwargs else tpl


class TelegramBotRunner:
    """Zero-dependency Telegram long-polling bot integrated with shared campaign state."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.stop_event = threading.Event()
        self.offset = 0

    def _call(self, method: str, payload: dict | None = None, timeout: int = 35) -> dict:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _download_file(self, file_id: str) -> str:
        info = self._call("getFile", {"file_id": file_id}, timeout=15)
        file_path = info["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.read().decode("utf-8-sig", errors="replace")

    def send_msg(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text[:4000]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = self._call("sendMessage", payload, timeout=15)
            return resp.get("result", {}).get("message_id")
        except Exception as exc:  # noqa: BLE001
            print(f"[tg_bot] sendMessage error: {exc}")
            return None

    def answer_cb(self, callback_id: str) -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def _main_keyboard(self, state: dict, chat_id: int) -> dict:
        return {
            "keyboard": [
                [{"text": _t(state, chat_id, "btn_start")}, {"text": _t(state, chat_id, "btn_status")}],
                [{"text": _t(state, chat_id, "btn_test")}, {"text": _t(state, chat_id, "btn_import")}],
                [{"text": _t(state, chat_id, "btn_inbox")}, {"text": _t(state, chat_id, "btn_blacklist")}],
                [{"text": _t(state, chat_id, "btn_lang")}],
            ],
            "resize_keyboard": True,
        }

    def _lang_inline(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "🇷🇺 Русский", "callback_data": "lang:ru"},
                    {"text": "🇬🇧 English", "callback_data": "lang:en"},
                    {"text": "🇮🇩 Bahasa ID", "callback_data": "lang:id"},
                ]
            ]
        }

    def _is_authorized(self, state: dict, chat_id: int, username: str) -> bool:
        if chat_id in state["authorized_chats"]:
            return True
        env = get_env()
        allowed_raw = env.get("TG_ALLOWED_USERS", "").strip()
        if not allowed_raw:
            return False
        allowed = {x.strip().lstrip("@").lower() for x in allowed_raw.split(",") if x.strip()}
        if str(chat_id) in allowed or (username and username.lower() in allowed):
            state["authorized_chats"].append(chat_id)
            _save_state(state)
            return True
        return False

    def _send_menu(self, state: dict, chat_id: int) -> None:
        env = get_env()
        try:
            acc = find_account(None, env)
            sender = f"{acc['sender_name']} <{acc['sender_email']}>"
        except Exception:  # noqa: BLE001
            sender = "not configured"

        sent_stats = get_sent_stats(load_sent_log())
        snap = STATE.snapshot()
        status_txt = (
            _t(state, chat_id, "status_running", cur=snap["progress_current"], tot=snap["progress_total"])
            if snap["running"]
            else _t(state, chat_id, "status_idle")
        )
        msg = _t(
            state,
            chat_id,
            "menu_title",
            sender=sender,
            today=sent_stats["sent_today"],
            total=sent_stats["total_sent"],
            status=status_txt,
        )
        self.send_msg(chat_id, msg, reply_markup=self._main_keyboard(state, chat_id))

    def _handle_callback(self, cb: dict) -> None:
        cb_id = cb["id"]
        msg = cb.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        data = str(cb.get("data") or "")
        user = cb.get("from") or {}
        username = str(user.get("username") or "")
        self.answer_cb(cb_id)
        if not chat_id:
            return

        state = _load_state()
        if data.startswith("lang:"):
            lang = data.split(":", 1)[1]
            if lang in STRINGS:
                state["chat_langs"][str(chat_id)] = lang
                _save_state(state)
                self.send_msg(chat_id, _t(state, chat_id, "lang_saved"))
                if self._is_authorized(state, chat_id, username):
                    self._send_menu(state, chat_id)
                else:
                    self.send_msg(chat_id, _t(state, chat_id, "ask_pin"))
            return

        if not self._is_authorized(state, chat_id, username):
            self.send_msg(chat_id, _t(state, chat_id, "ask_pin"))
            return

        if data == "cancel":
            state["pending"].pop(str(chat_id), None)
            _save_state(state)
            self._send_menu(state, chat_id)
            return

        if data == "stop_run":
            STATE.stop_event.set()
            self.send_msg(chat_id, _t(state, chat_id, "stopped"))
            return

        if data.startswith("wiz_mkt:"):
            mkt = data.split(":", 1)[1]
            state["pending"][str(chat_id)] = {"mode": "wiz", "market": mkt}
            _save_state(state)
            self.send_msg(
                chat_id,
                _t(state, chat_id, "wiz_site"),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": _t(state, chat_id, "site_no"), "callback_data": "wiz_site:no_site"},
                            {"text": _t(state, chat_id, "site_has"), "callback_data": "wiz_site:has_site"},
                        ],
                        [{"text": _t(state, chat_id, "site_all"), "callback_data": "wiz_site:ALL"}],
                        [{"text": _t(state, chat_id, "btn_cancel"), "callback_data": "cancel"}],
                    ]
                },
            )
            return

        if data.startswith("wiz_site:"):
            site = data.split(":", 1)[1]
            pend = state["pending"].get(str(chat_id), {})
            pend["site_status"] = site
            state["pending"][str(chat_id)] = pend
            _save_state(state)
            self.send_msg(
                chat_id,
                _t(state, chat_id, "wiz_limit"),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "10", "callback_data": "wiz_lim:10"},
                            {"text": "20", "callback_data": "wiz_lim:20"},
                            {"text": "35", "callback_data": "wiz_lim:35"},
                        ],
                        [{"text": _t(state, chat_id, "btn_cancel"), "callback_data": "cancel"}],
                    ]
                },
            )
            return

        if data.startswith("wiz_lim:"):
            limit = int(data.split(":", 1)[1])
            pend = state["pending"].get(str(chat_id), {})
            pend["limit"] = limit
            state["pending"][str(chat_id)] = pend
            _save_state(state)
            self._show_wizard_confirm(state, chat_id, pend)
            return

        if data == "wiz_go":
            pend = state["pending"].pop(str(chat_id), {})
            _save_state(state)
            try:
                def _done(res: dict) -> None:
                    self.send_msg(
                        chat_id,
                        f"🏁 Campaign finished: sent={res.get('sent', 0)}, errors={res.get('errors', 0)}",
                    )

                start_campaign_async(
                    market=pend.get("market", "ALL"),
                    site_status=pend.get("site_status", "ALL"),
                    daily_limit=int(pend.get("limit", 20)),
                    on_complete=_done,
                )
                self.send_msg(chat_id, _t(state, chat_id, "started"))
            except Exception as exc:  # noqa: BLE001
                self.send_msg(chat_id, _t(state, chat_id, "err_prefix", err=str(exc)))
            return

        if data.startswith("rpl:"):
            rid = data.split(":", 1)[1]
            rep = state["replies"].get(rid)
            if rep:
                state["pending"][str(chat_id)] = {"mode": "reply", "rid": rid}
                _save_state(state)
                self.send_msg(
                    chat_id,
                    _t(state, chat_id, "ask_reply_text", email=rep["from_email"], subject=rep["subject"]),
                )
            return

        if data.startswith("blk:"):
            rid = data.split(":", 1)[1]
            rep = state["replies"].get(rid)
            if rep:
                bl = load_blacklist()
                bl.add(rep["from_email"].lower())
                save_blacklist(list(bl))
                upsert_crm_contact(
                    rep["from_email"],
                    "BLOCKED",
                    name=rep.get("from_name", ""),
                    subject=rep.get("subject", ""),
                )
                self.send_msg(chat_id, _t(state, chat_id, "blacklist_done", entry=rep["from_email"]))
            return

        if data.startswith("lead:"):
            rid = data.split(":", 1)[1]
            rep = state["replies"].get(rid)
            if rep:
                upsert_crm_contact(
                    rep["from_email"],
                    "INTERESTED",
                    name=rep.get("from_name", ""),
                    subject=rep.get("subject", ""),
                    last_message=rep.get("body", ""),
                )
                self.send_msg(chat_id, _t(state, chat_id, "lead_saved", email=rep["from_email"]))
            return

        if data.startswith("done:"):
            rid = data.split(":", 1)[1]
            rep = state["replies"].get(rid)
            if rep:
                upsert_crm_contact(
                    rep["from_email"],
                    "CLOSED",
                    name=rep.get("from_name", ""),
                    subject=rep.get("subject", ""),
                )
                self.send_msg(chat_id, _t(state, chat_id, "done_saved", email=rep["from_email"]))
            return

    def _show_wizard_confirm(self, state: dict, chat_id: int, pend: dict) -> None:
        env = get_env()
        acc = find_account(None, env)
        bvars = brand_vars(env, acc["sender_name"], acc["sender_email"])
        mkt = pend.get("market", "ALL")
        site = pend.get("site_status", "ALL")
        lim = int(pend.get("limit", 20))
        source_path = resolve_source_path(None)
        queue, _ = filter_queue(source_path, bvars, market_filter=mkt, site_filter=site)
        if not queue:
            self.send_msg(chat_id, _t(state, chat_id, "wiz_empty"))
            return

        first = queue[0]
        safety_res = preview_template_safety(
            market=first["market"] or "EN",
            site_status=first["site_status"] or "no_site",
        )
        safety = safety_res["safety"]
        if not safety["ok"]:
            self.send_msg(
                chat_id,
                _t(state, chat_id, "wiz_blocked", errors="\n".join(safety["errors"])),
            )
            return

        text = _t(
            state,
            chat_id,
            "wiz_confirm",
            market=mkt,
            site=site,
            queued=len(queue),
            limit=lim,
            min_d=env.get("MIN_DELAY_SEC", "90"),
            max_d=env.get("MAX_DELAY_SEC", "180"),
            sender=f"{acc['sender_name']} <{acc['sender_email']}>",
            score=safety["spam_score"],
            subject=safety_res["rendered_subject"],
            preview=str(safety_res["rendered_body"])[:500],
        )
        self.send_msg(
            chat_id,
            text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": _t(state, chat_id, "btn_confirm"), "callback_data": "wiz_go"},
                        {"text": _t(state, chat_id, "btn_cancel"), "callback_data": "cancel"},
                    ]
                ]
            },
        )

    def _handle_message(self, msg: dict) -> None:
        chat_id = msg.get("chat", {}).get("id")
        if not chat_id:
            return
        user = msg.get("from") or {}
        username = str(user.get("username") or "")
        text = str(msg.get("text") or "").strip()
        doc = msg.get("document")

        state = _load_state()

        if text in ("/start", "/lang"):
            if text == "/start" and not self._is_authorized(state, chat_id, username):
                self.send_msg(chat_id, _t(state, chat_id, "choose_lang"), reply_markup=self._lang_inline())
                return
            if text == "/lang":
                self.send_msg(chat_id, _t(state, chat_id, "choose_lang"), reply_markup=self._lang_inline())
                return
            self._send_menu(state, chat_id)
            return

        if not self._is_authorized(state, chat_id, username):
            expected_pin = get_env().get("TG_ACCESS_CODE", "").strip()
            if expected_pin and text == expected_pin:
                state["authorized_chats"].append(chat_id)
                _save_state(state)
                self.send_msg(chat_id, _t(state, chat_id, "auth_ok"))
                self._send_menu(state, chat_id)
            else:
                self.send_msg(chat_id, _t(state, chat_id, "bad_pin"))
            return

        # Instant swipe-reply in Telegram to any forwarded client message
        reply_to_msg = msg.get("reply_to_message")
        if reply_to_msg and text:
            map_key = f"{chat_id}:{reply_to_msg.get('message_id')}"
            rid = state.get("tg_msg_map", {}).get(map_key)
            if rid and rid in state.get("replies", {}):
                self._send_client_reply(state, chat_id, rid, text)
                return

        # Language button across any active language
        if text in {b["btn_lang"] for b in STRINGS.values()}:
            self.send_msg(chat_id, _t(state, chat_id, "choose_lang"), reply_markup=self._lang_inline())
            return

        # Menu buttons across any active language
        if text in {b["btn_start"] for b in STRINGS.values()}:
            if STATE.snapshot()["running"]:
                self.send_msg(chat_id, _t(state, chat_id, "already_running"))
                return
            self.send_msg(
                chat_id,
                _t(state, chat_id, "wiz_market"),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🇬🇧 EN", "callback_data": "wiz_mkt:EN"},
                            {"text": "🇮🇩 ID", "callback_data": "wiz_mkt:ID"},
                            {"text": "🇷🇺 RU", "callback_data": "wiz_mkt:RU"},
                            {"text": "🇺🇦 UA", "callback_data": "wiz_mkt:UA"},
                        ],
                        [{"text": _t(state, chat_id, "mkt_all"), "callback_data": "wiz_mkt:ALL"}],
                        [{"text": _t(state, chat_id, "btn_cancel"), "callback_data": "cancel"}],
                    ]
                },
            )
            return

        if text in {b["btn_status"] for b in STRINGS.values()}:
            snap = STATE.snapshot()
            kb = {"inline_keyboard": [[{"text": _t(state, chat_id, "btn_stop"), "callback_data": "stop_run"}]]} if snap["running"] else None
            sent_stats = get_sent_stats(load_sent_log())
            logs_tail = "\n".join(snap["logs"][-10:]) if snap["logs"] else ""
            body = (
                f"Today: {sent_stats['sent_today']} | Total: {sent_stats['total_sent']}\n"
                f"Progress: {snap['progress_current']}/{snap['progress_total']}\n"
                + (f"\n{logs_tail}" if logs_tail else "")
            )
            self.send_msg(chat_id, body, reply_markup=kb)
            return

        if text in {b["btn_test"] for b in STRINGS.values()}:
            state["pending"][str(chat_id)] = {"mode": "test_email"}
            _save_state(state)
            self.send_msg(chat_id, _t(state, chat_id, "ask_test_email"))
            return

        if text in {b["btn_import"] for b in STRINGS.values()}:
            state["pending"][str(chat_id)] = {"mode": "import_leads"}
            _save_state(state)
            self.send_msg(chat_id, _t(state, chat_id, "ask_import"))
            return

        if text in {b["btn_blacklist"] for b in STRINGS.values()}:
            state["pending"][str(chat_id)] = {"mode": "blacklist"}
            _save_state(state)
            self.send_msg(chat_id, _t(state, chat_id, "ask_blacklist"))
            return

        if text in {b["btn_inbox"] for b in STRINGS.values()}:
            imap_cfg = load_imap_config()
            crm = load_crm_contacts()
            crm_lines = []
            for entry in sorted(crm.values(), key=lambda x: x.get("updated_at", ""), reverse=True)[:10]:
                crm_lines.append(f"• {entry['email']} [{entry.get('status', 'REPLIED')}] — {entry.get('subject', '')[:45]}")
            crm_block = ("\n\n📋 CRM:\n" + "\n".join(crm_lines)) if crm_lines else ""
            if imap_cfg.get("user") and imap_cfg.get("password"):
                self.send_msg(chat_id, _t(state, chat_id, "inbox_ready", user=imap_cfg["user"]) + crm_block)
            else:
                reply_to = get_env().get("REPLY_TO_EMAIL") or get_env().get("SENDER_EMAIL") or "-"
                self.send_msg(chat_id, _t(state, chat_id, "inbox_not_cfg", reply_to=reply_to) + crm_block)
            return

        # Pending interactive input states
        pend = state["pending"].get(str(chat_id))
        if doc and (pend and pend.get("mode") == "import_leads" or not pend):
            try:
                raw_csv = self._download_file(doc["file_id"])
                target = "leads_ready.csv" if LEADS_READY_PATH.exists() else "contacts.csv"
                res = import_leads_text(raw_csv, target_name=target)
                state["pending"].pop(str(chat_id), None)
                _save_state(state)
                self.send_msg(
                    chat_id,
                    _t(
                        state,
                        chat_id,
                        "import_done",
                        target=res["target"],
                        added=res["added"],
                        dup=res["duplicates"],
                        syn=res["invalid_syntax"],
                        mx=res["invalid_mx"],
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self.send_msg(chat_id, _t(state, chat_id, "err_prefix", err=str(exc)))
            return

        if not pend:
            self._send_menu(state, chat_id)
            return

        mode = pend.get("mode")
        if mode == "test_email":
            state["pending"].pop(str(chat_id), None)
            _save_state(state)
            try:
                env = get_env()
                acc = find_account(None, env)
                bvars = brand_vars(env, acc["sender_name"], acc["sender_email"])
                lang = state.get("chat_langs", {}).get(str(chat_id), "en").upper()
                _, subj_tpl, body_tpl = select_template(load_templates_config(), market=lang, site_status="no_site", template_id=None)
                subj, txt, html_body = render_message(subj_tpl, body_tpl, {**bvars, "name": "Alex", "company": "Demo Auto Service", "email": text})
                safety = check_message_safety(subj_tpl, body_tpl, subj, txt)
                if not safety["ok"]:
                    raise ValueError("; ".join(safety["errors"]))  # type: ignore[arg-type]
                res = send_via_account(acc, text.strip(), subj, txt, html_body)
                self.send_msg(chat_id, _t(state, chat_id, "test_sent", email=text.strip(), id=res.get("id")))
            except Exception as exc:  # noqa: BLE001
                self.send_msg(chat_id, _t(state, chat_id, "err_prefix", err=str(exc)))
            return

        if mode == "import_leads" and text:
            state["pending"].pop(str(chat_id), None)
            _save_state(state)
            try:
                target = "contacts.csv" if CONTACTS_PATH.exists() else "leads_ready.csv"
                res = import_leads_text(text, target_name=target)
                self.send_msg(
                    chat_id,
                    _t(
                        state,
                        chat_id,
                        "import_done",
                        target=res["target"],
                        added=res["added"],
                        dup=res["duplicates"],
                        syn=res["invalid_syntax"],
                        mx=res["invalid_mx"],
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self.send_msg(chat_id, _t(state, chat_id, "err_prefix", err=str(exc)))
            return

        if mode == "blacklist" and text:
            state["pending"].pop(str(chat_id), None)
            _save_state(state)
            bl = load_blacklist()
            entry = text.strip().lower()
            bl.add(entry)
            save_blacklist(list(bl))
            self.send_msg(chat_id, _t(state, chat_id, "blacklist_done", entry=entry))
            return

        if mode == "reply" and text:
            rid = pend.get("rid", "")
            state["pending"].pop(str(chat_id), None)
            _save_state(state)
            if rid in state.get("replies", {}):
                self._send_client_reply(state, chat_id, rid, text)
            return

    def _send_client_reply(self, state: dict, chat_id: int, rid: str, reply_text: str) -> None:
        """Send threaded email reply and record contact status in CRM."""
        rep = state["replies"].get(rid)
        if not rep:
            return
        try:
            env = get_env()
            acc = find_account(None, env)
            send_reply_email(
                to_email=rep["from_email"],
                subject=rep["subject"],
                reply_text=reply_text,
                in_reply_to=rep.get("message_id", ""),
                references=rep.get("references", ""),
            )
            upsert_crm_contact(
                rep["from_email"],
                "REPLIED_BY_US",
                name=rep.get("from_name", ""),
                subject=rep.get("subject", ""),
                last_message=reply_text,
            )
            sender = f"{acc['sender_name']} <{acc['sender_email']}>"
            self.send_msg(chat_id, _t(state, chat_id, "reply_sent", email=rep["from_email"], sender=sender))
        except Exception as exc:  # noqa: BLE001
            self.send_msg(chat_id, _t(state, chat_id, "err_prefix", err=str(exc)))

    def _imap_poll_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                state = _load_state()
                if state["authorized_chats"]:
                    last_uid = int(state.get("last_imap_uid") or 0)
                    new_max, replies = fetch_new_imap_replies(last_uid)
                    if new_max != last_uid or replies:
                        for rep in replies:
                            rid = str(state["next_reply_id"])
                            state["next_reply_id"] += 1
                            state["replies"][rid] = rep
                            upsert_crm_contact(
                                rep["from_email"],
                                "REPLIED",
                                name=rep.get("from_name", ""),
                                subject=rep.get("subject", ""),
                                last_message=rep.get("body", ""),
                            )
                            if len(state["replies"]) > 100:
                                oldest = sorted(state["replies"].keys(), key=int)[0]
                                state["replies"].pop(oldest, None)
                            for cid in state["authorized_chats"]:
                                tg_mid = self.send_msg(
                                    cid,
                                    _t(
                                        state,
                                        cid,
                                        "new_reply",
                                        name=rep["from_name"],
                                        email=rep["from_email"],
                                        subject=rep["subject"],
                                        body=rep["body"][:1200],
                                    ),
                                    reply_markup={
                                        "inline_keyboard": [
                                            [
                                                {"text": _t(state, cid, "btn_reply"), "callback_data": f"rpl:{rid}"},
                                                {"text": _t(state, cid, "btn_lead"), "callback_data": f"lead:{rid}"},
                                            ],
                                            [
                                                {"text": _t(state, cid, "btn_done"), "callback_data": f"done:{rid}"},
                                                {"text": _t(state, cid, "btn_block_sender"), "callback_data": f"blk:{rid}"},
                                            ],
                                        ]
                                    },
                                )
                                if tg_mid:
                                    state["tg_msg_map"][f"{cid}:{tg_mid}"] = rid
                                    if len(state["tg_msg_map"]) > 200:
                                        first_k = next(iter(state["tg_msg_map"]))
                                        state["tg_msg_map"].pop(first_k, None)
                        state["last_imap_uid"] = new_max
                        _save_state(state)
            except Exception as exc:  # noqa: BLE001
                print(f"[tg_bot] IMAP poll warning: {exc}")
            self.stop_event.wait(45)

    def run_forever(self) -> None:
        """Start IMAP poller thread and Telegram getUpdates polling loop."""
        threading.Thread(target=self._imap_poll_loop, daemon=True).start()
        print("Telegram bot polling started.")
        while not self.stop_event.is_set():
            try:
                qs = urllib.parse.urlencode({"offset": self.offset, "timeout": 25})
                with urllib.request.urlopen(f"{self.base_url}/getUpdates?{qs}", timeout=35) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    if "callback_query" in upd:
                        self._handle_callback(upd["callback_query"])
                    elif "message" in upd:
                        self._handle_message(upd["message"])
            except Exception as exc:  # noqa: BLE001
                print(f"[tg_bot] poll error: {exc}")
                self.stop_event.wait(3)


def start_bot_in_background() -> TelegramBotRunner | None:
    """Start Telegram bot thread if TG_BOT_TOKEN is configured."""
    token = get_env().get("TG_BOT_TOKEN", "").strip()
    if not token:
        return None
    runner = TelegramBotRunner(token)
    threading.Thread(target=runner.run_forever, daemon=True).start()
    return runner


if __name__ == "__main__":
    token = get_env().get("TG_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TG_BOT_TOKEN is not set in .env")
    TelegramBotRunner(token).run_forever()

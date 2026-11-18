"""Anti-spam guardrails, preflight content checks, pacing clamps, and blacklists."""

from __future__ import annotations

from pathlib import Path
import os
import re
import string

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
BLACKLIST_PATH = DATA_DIR / "blacklist.txt"

PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
SHORTENER_DOMAINS = (
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "clck.ru",
    "goo.su",
    "cutt.ly",
    "rb.gy",
    "ow.ly",
)
SPAM_PHRASES = (
    "free money",
    "100% free",
    "100% guarantee",
    "act now",
    "limited time offer",
    "click here now",
    "buy now",
    "earn extra cash",
    "risk free",
    "no obligation",
    "winner",
    "congratulations you won",
    "casino",
    "crypto giveaway",
    "срочно жми",
    "бесплатные деньги",
    "гарантия 100%",
    "заработок без вложений",
    "только сегодня скидка",
    "вы выиграли",
)
KNOWN_VARIABLES = {
    "company",
    "name",
    "email",
    "brand_name",
    "brand_url",
    "city",
    "niche",
    "country",
    "market",
    "site_url",
}

MIN_DELAY_FLOOR_SEC = 45
HARD_DAILY_LIMIT_RESEND = 50
HARD_DAILY_LIMIT_SMTP = 30


def load_blacklist(path: Path = BLACKLIST_PATH) -> set[str]:
    """Load lowercase blocked emails and @domain rules from blacklist.txt."""
    if not path.exists():
        return set()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        item = raw.strip().lower()
        if item and not item.startswith("#"):
            entries.add(item)
    return entries


def save_blacklist(entries: list[str], path: Path = BLACKLIST_PATH) -> list[str]:
    """Normalize, deduplicate, and persist blacklist.txt."""
    cleaned = sorted({e.strip().lower() for e in entries if e and e.strip()})
    path.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
    return cleaned


def is_blacklisted(email: str, blacklist: set[str]) -> bool:
    """Return True if exact email or its @domain is in blacklist."""
    norm = email.strip().lower()
    if not norm or "@" not in norm:
        return True
    domain = "@" + norm.split("@", 1)[1]
    return norm in blacklist or domain in blacklist


def enforce_safe_pacing(
    provider: str,
    daily_limit: int,
    min_delay: int,
    max_delay: int,
) -> tuple[int, int, int, list[str]]:
    """Clamp daily limits and delays to safe anti-spam bounds."""
    notices: list[str] = []
    cap = HARD_DAILY_LIMIT_SMTP if provider == "smtp" else HARD_DAILY_LIMIT_RESEND

    safe_limit = max(1, daily_limit)
    if safe_limit > cap:
        notices.append(
            f"Дневной лимит снижен с {safe_limit} до безопасного потолка {cap} для {provider.upper()}."
        )
        safe_limit = cap

    safe_min = max(MIN_DELAY_FLOOR_SEC, min_delay)
    if min_delay < MIN_DELAY_FLOOR_SEC:
        notices.append(
            f"Минимальная пауза поднята с {min_delay}с до безопасных {MIN_DELAY_FLOOR_SEC}с."
        )

    safe_max = max(safe_min, max_delay)
    if safe_max < 90:
        safe_max = max(safe_min, 90)
    return safe_limit, safe_min, safe_max, notices


def validate_template_syntax(text: str) -> list[str]:
    """Check for unbalanced braces or unknown {placeholders} before sending."""
    errors: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                errors.append("Лишняя закрывающая скобка '}' в шаблоне.")
                return errors
    if depth != 0:
        errors.append("Незакрытая фигурная скобка '{' в шаблоне.")
        return errors

    for match in PLACEHOLDER_RE.finditer(text):
        token = match.group(1).strip()
        if "|" in token:
            options = [opt.strip() for opt in token.split("|")]
            if any(not opt for opt in options):
                errors.append(f"Пустой вариант в спинтаксе: {{{token}}}")
            continue
        if token not in KNOWN_VARIABLES:
            errors.append(f"Неизвестная переменная '{{{token}}}' — проверьте опечатку.")
    return errors


def check_message_safety(
    subject_tpl: str,
    body_tpl: str,
    rendered_subject: str,
    rendered_body: str,
) -> dict[str, object]:
    """Evaluate rendered email + template syntax for spam risks and worker typos."""
    errors: list[str] = []
    warnings: list[str] = []
    score = 0

    for label, raw in (("Теме", subject_tpl), ("Тексте", body_tpl)):
        for err in validate_template_syntax(raw):
            errors.append(f"{label}: {err}")

    if not rendered_subject.strip():
        errors.append("Тема письма пустая.")
    elif len(rendered_subject.strip()) < 5:
        errors.append("Тема письма слишком короткая (меньше 5 символов).")

    body_clean = rendered_body.strip()
    if len(body_clean) < 25:
        errors.append("Текст письма слишком короткий (меньше 25 символов).")
    elif len(body_clean) > 3500:
        warnings.append("Письмо слишком длинное (>3500 символов) — для холодной рассылки лучше короче.")
        score += 15

    # Check unresolved brace leftovers after rendering.
    if "{" in rendered_subject or "}" in rendered_subject or "{" in body_clean or "}" in body_clean:
        errors.append("В итоговом письме остались нераскрытые скобки '{' или '}'.")

    # ALL CAPS ratio check.
    subj_letters = [c for c in rendered_subject if c.isalpha()]
    if len(subj_letters) >= 6:
        upper_ratio = sum(1 for c in subj_letters if c.isupper()) / len(subj_letters)
        if upper_ratio > 0.5:
            score += 30
            warnings.append("Слишком много ЗАГЛАВНЫХ БУКВ в теме письма (высокий риск спама).")

    body_letters = [c for c in body_clean if c.isalpha()]
    if len(body_letters) >= 20:
        upper_ratio = sum(1 for c in body_letters if c.isupper()) / len(body_letters)
        if upper_ratio > 0.35:
            score += 30
            warnings.append("Слишком много ЗАГЛАВНЫХ БУКВ в тексте письма.")

    # Excessive punctuation.
    if re.search(r"[!?]{3,}", rendered_subject + " " + body_clean):
        score += 20
        warnings.append("Уберите лишние восклицательные/вопросительные знаки подряд (!!! / ???).")

    # Links & URL shorteners.
    urls = URL_RE.findall(body_clean)
    if len(urls) > 2:
        score += 25
        warnings.append(f"В письме {len(urls)} ссылок — для первого холодного письма оставьте не больше 1–2.")
    for u in urls:
        lower_u = u.lower()
        if any(short in lower_u for short in SHORTENER_DOMAINS):
            score += 40
            errors.append(f"Запрещённый сокращатель ссылок ({u}) — почтовики сразу кидают такие письма в спам.")

    # Classic spam trigger phrases.
    combined_lower = (rendered_subject + "\n" + body_clean).lower()
    matched_phrases = [p for p in SPAM_PHRASES if p in combined_lower]
    if matched_phrases:
        score += 25 * len(matched_phrases)
        warnings.append(
            "Найдены стоп-слова спам-фильтров: " + ", ".join(matched_phrases[:5])
        )

    # Check unresolved generic placeholders left accidentally.
    for placeholder_word in ("yourdomain.com", "your_api_key", "TestCompany"):
        if placeholder_word.lower() in combined_lower:
            errors.append(f"В письме осталась тестовая заглушка '{placeholder_word}'.")

    # Reject control characters (except newlines/tabs).
    printable_set = set(string.printable)
    if any(ord(ch) < 32 and ch not in ("\n", "\r", "\t") for ch in body_clean):
        errors.append("В тексте обнаружены скрытые управляющие символы.")

    if score >= 60 and not errors:
        errors.append(
            f"Слишком высокий спам-рейтинг письма ({score}/100). Исправьте замечания выше перед запуском."
        )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "spam_score": min(100, score),
    }

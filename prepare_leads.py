"""Prepare and DNS-MX validate email leads from CSV exports."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

from guardrails import BASE_DIR, DATA_DIR
from mailer import ENV_PATH, load_env

READY_CSV = DATA_DIR / "leads_ready.csv"
INVALID_CSV = DATA_DIR / "leads_invalid.csv"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
JUNK_LOCAL_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon")
JUNK_DOMAINS = {"example.com", "example.org", "example.net", "test.com", "sentry.io", "wixpress.com"}
COMMON_VALID_DOMAINS = {
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
}


def query_doh_records(name: str, rtype: str, timeout: int = 8) -> tuple[int, list[str]]:
    """Query Cloudflare DNS-over-HTTPS JSON endpoint and return (rcode, record_values)."""
    url = (
        f"https://cloudflare-dns.com/dns-query"
        f"?name={urllib.parse.quote(name)}&type={urllib.parse.quote(rtype)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/dns-json",
            "User-Agent": "outreach-mailer/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    status = int(data.get("Status") if data.get("Status") is not None else -1)
    type_map = {"TXT": 16, "MX": 15, "CNAME": 5, "A": 1}
    want_type = type_map.get(rtype.upper())
    answers = data.get("Answer") or []
    values = [
        str(a.get("data") or "").strip()
        for a in answers
        if want_type is None or a.get("type") == want_type
    ]
    return status, [v for v in values if v]


def check_domain_mx(domain: str) -> tuple[str, bool, str]:
    """Return (domain, has_mx, detail) using DNS-over-HTTPS with nslookup fallback."""
    if domain in COMMON_VALID_DOMAINS:
        return domain, True, "known_provider"

    try:
        status, mx_answers = query_doh_records(domain, "MX", timeout=8)
        if status == 0 and mx_answers:
            return domain, True, "doh_mx"
        if status == 3:
            return domain, False, "nxdomain"
        return domain, False, f"no_mx_status_{status}"
    except Exception:  # noqa: BLE001
        pass

    try:
        proc = subprocess.run(
            ["nslookup", "-type=mx", domain, "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        out = (proc.stdout or "").lower()
        if "mail exchanger" in out or "mx preference" in out:
            return domain, True, "nslookup_mx"
        return domain, False, "nslookup_no_mx"
    except Exception as exc:  # noqa: BLE001
        return domain, False, f"lookup_error:{exc}"


def is_clean_syntax(email: str) -> tuple[bool, str]:
    """Check basic email syntax and skip obvious junk/placeholder addresses."""
    if not EMAIL_RE.match(email):
        return False, "bad_syntax"
    local, domain = email.split("@", 1)
    if local in JUNK_LOCAL_PREFIXES or domain in JUNK_DOMAINS:
        return False, "junk_address"
    if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return False, "asset_filename"
    return True, "ok"


def resolve_leads_dir(cli_dir: str | None) -> Path:
    """Resolve input directory from --leads-dir, .env LEADS_DIR, or ./raw_leads."""
    env = {**load_env(ENV_PATH), **os.environ}
    raw = (cli_dir or env.get("LEADS_DIR") or "./raw_leads").strip()
    path = Path(raw)
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def main() -> int:
    """Deduplicate, validate MX records, and write leads_ready.csv + leads_invalid.csv."""
    parser = argparse.ArgumentParser(description="Validate CSV lead emails via DNS MX lookup")
    parser.add_argument("--leads-dir", help="Directory containing source CSV files")
    args = parser.parse_args()

    leads_dir = resolve_leads_dir(args.leads_dir)
    csv_files = sorted(leads_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {leads_dir}")

    seen_emails: set[str] = set()
    candidates: list[dict[str, str]] = []
    invalid_rows: list[dict[str, str]] = []
    duplicates = 0

    for csv_path in csv_files:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh):
                email = (raw.get("Email") or raw.get("email") or "").strip().lower()
                if not email:
                    continue
                if email in seen_emails:
                    duplicates += 1
                    continue
                seen_emails.add(email)

                company = (raw.get("Company_Name") or raw.get("company") or "").strip() or "your team"
                site_status = (raw.get("Site_Status") or raw.get("site_status") or "has_site").strip()
                if site_status not in ("no_site", "has_site"):
                    site_status = "has_site"

                record = {
                    "email": email,
                    "company": company,
                    "market": (raw.get("Market") or raw.get("market") or "EN").strip().upper(),
                    "country": (raw.get("Country") or raw.get("country") or "").strip(),
                    "city": (raw.get("City") or raw.get("city") or "").strip(),
                    "niche": (raw.get("Niche") or raw.get("niche") or "").strip(),
                    "site_status": site_status,
                    "site_url": (raw.get("Site_URL") or raw.get("site_url") or "").strip(),
                    "phone": (raw.get("Phone_Formatted") or raw.get("phone") or "").strip(),
                    "source": (raw.get("Source") or raw.get("source") or csv_path.name).strip(),
                }

                ok, reason = is_clean_syntax(email)
                if not ok:
                    invalid_rows.append({**record, "reason": reason})
                else:
                    candidates.append(record)

    unique_domains = sorted({item["email"].split("@", 1)[1] for item in candidates})
    print(f"Checking MX records for {len(unique_domains)} unique domains...")

    domain_status: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(check_domain_mx, d) for d in unique_domains]
        for fut in as_completed(futures):
            dom, has_mx, detail = fut.result()
            domain_status[dom] = (has_mx, detail)

    ready_rows: list[dict[str, str]] = []
    for item in candidates:
        dom = item["email"].split("@", 1)[1]
        has_mx, detail = domain_status.get(dom, (False, "unknown"))
        if has_mx:
            ready_rows.append(item)
        else:
            invalid_rows.append({**item, "reason": detail})

    ready_rows.sort(key=lambda r: (r["market"], 0 if r["site_status"] == "no_site" else 1, r["company"]))

    ready_fields = [
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
    with READY_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ready_fields)
        writer.writeheader()
        writer.writerows(ready_rows)

    invalid_fields = [*ready_fields, "reason"]
    with INVALID_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=invalid_fields)
        writer.writeheader()
        writer.writerows(invalid_rows)

    print(f"Duplicates skipped: {duplicates}")
    print(f"Ready leads (valid MX): {len(ready_rows)} -> {READY_CSV}")
    print(f"Filtered out (bad/no MX): {len(invalid_rows)} -> {INVALID_CSV}")

    for mkt in sorted({r["market"] for r in ready_rows}):
        subset = [r for r in ready_rows if r["market"] == mkt]
        no_site = sum(1 for r in subset if r["site_status"] == "no_site")
        has_site = len(subset) - no_site
        print(f"  {mkt}: {len(subset)} ready (no_site={no_site}, has_site={has_site})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

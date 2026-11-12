#!/usr/bin/env python3
"""Local discovery from LinkedIn's public guest job search (no login, no account). Slow on purpose.

Searches each query for the last N days, keeps new cards (not in queue.json or Applications.xlsx by
company+title), and writes data/linkedin_candidates.json for manual triage. LinkedIn 429s fast and blocks
shared runner IPs, so run this locally and never from the hourly job.

    py -3 linkedin_guest_search.py [--days 14] [--pages 2] [--dry-run]
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import atomic_json

# --- config ---
QUERIES = [
    "asset liability management analyst", "liquidity risk analyst", "treasury risk analyst",
    "funds transfer pricing", "interest rate risk analyst", "market risk analyst",
    "model validation analyst", "regulatory capital analyst", "securitized products analyst",
    "structured finance analyst", "fixed income analyst", "credit risk analyst bank",
]
LOCATION = "United States"
PAGE_SIZE = 10          # guest endpoint returns 10 cards per call here
PAUSE_S = 5
BACKOFF_S = 90
MAX_429_STREAK = 2
TRACKER = Path(r"D:\Code\Resume\Applications.xlsx")
QUEUE = Path(__file__).resolve().parent / "data" / "queue.json"
CONFIG = Path(__file__).resolve().parent / "data" / "config.json"
OUT = Path(__file__).resolve().parent / "data" / "linkedin_candidates.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
# --- end config ---

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def norm(s):
    return re.sub(r"[^a-z0-9]", "", html.unescape(s or "").lower())


def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "replace")


def parse_cards(page):
    out = []
    for card in re.split(r"<li>", page)[1:]:
        m = re.search(r"urn:li:jobPosting:(\d+)", card)
        if not m:
            continue
        def g(pat):
            x = re.search(pat, card, re.S)
            return html.unescape(re.sub(r"<[^>]+>", "", x.group(1))).strip() if x else ""
        out.append({
            "id": m.group(1),
            "title": g(r'base-search-card__title[^>]*>(.*?)</h3>'),
            "company": g(r'base-search-card__subtitle[^>]*>(.*?)</h4>'),
            "location": g(r'job-search-card__location[^>]*>(.*?)</span>'),
            "posted": (re.search(r'datetime="([\d-]+)"', card) or [None, ""])[1],
            "link": f"https://www.linkedin.com/jobs/view/{m.group(1)}",
        })
    return out


def board_companies():
    """Normalized names of companies the scout already polls; LinkedIn is only for the rest."""
    try:
        return {norm(c["name"]) for c in json.loads(CONFIG.read_text(encoding="utf-8"))["companies"]}
    except Exception as e:  # noqa: BLE001
        print("config not read:", e, file=sys.stderr)
        return set()


def covered(company, boards):
    n = norm(company)
    return any(n == b or (len(b) > 4 and (n.startswith(b) or b.startswith(n))) for b in boards)


def known_keys():
    keys = set()
    try:
        for r in json.loads(QUEUE.read_text(encoding="utf-8")):
            keys.add((norm(r.get("company")), norm(r.get("role"))))
    except Exception as e:  # noqa: BLE001
        print("queue not read:", e, file=sys.stderr)
    try:
        import openpyxl
        ws = openpyxl.load_workbook(TRACKER, read_only=True, data_only=True).active
        rows = ws.iter_rows(values_only=True)
        head = list(next(rows))
        c, t = head.index("Company"), head.index("Role Title")
        for r in rows:
            if r[c]:
                keys.add((norm(r[c]), norm(r[t])))
    except Exception as e:  # noqa: BLE001
        print("tracker not read:", e, file=sys.stderr)
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--all", action="store_true", help="keep roles at companies the scout already polls")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    seen_ids, found, streak = set(), [], 0
    known = known_keys()
    for q in QUERIES:
        for p in range(a.pages):
            params = {"keywords": q, "location": LOCATION, "f_TPR": f"r{a.days * 86400}", "f_JT": "F", "start": p * PAGE_SIZE}
            try:
                cards = parse_cards(fetch(SEARCH + "?" + urllib.parse.urlencode(params)))
                streak = 0
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    streak += 1
                    print(f"429 on '{q}' page {p}; backing off {BACKOFF_S}s", file=sys.stderr)
                    if streak >= MAX_429_STREAK:
                        print("stopping: repeated 429", file=sys.stderr)
                        break
                    time.sleep(BACKOFF_S)
                else:
                    print(f"HTTP {e.code} on '{q}'", file=sys.stderr)
                cards = []
            if not cards:
                break
            for c in cards:
                if c["id"] in seen_ids:
                    continue
                seen_ids.add(c["id"])
                c["query"] = q
                c["tracked"] = (norm(c["company"]), norm(c["title"])) in known
                found.append(c)
            time.sleep(PAUSE_S)
        if streak >= MAX_429_STREAK:
            break
    fresh = [c for c in found if not c["tracked"]]
    boards = board_companies()
    for c in fresh:
        c["has_board"] = covered(c["company"], boards)
    skipped = sum(c["has_board"] for c in fresh)
    if not a.all:  # the scout already sees these through the company board
        fresh = [c for c in fresh if not c["has_board"]]
        print(f"{skipped} dropped: company already has a board in config")
    print(f"{len(found)} cards, {len(fresh)} not already queued or tracked")
    if not a.dry_run:
        atomic_json.dump(fresh, OUT) if hasattr(atomic_json, "dump") else OUT.write_text(json.dumps(fresh, indent=1), encoding="utf-8")
        print("wrote", OUT)
    for c in fresh[:40]:
        print(f"{c['company'][:28]:28}|{c['title'][:55]:55}|{c['location'][:22]:22}|{c['posted']}")


if __name__ == "__main__":
    main()

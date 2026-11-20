#!/usr/bin/env python3
"""Keep data/unsupported.json (the page's 'Not covered' list) true: drop employers that are now polled,
add employers found to have a careers site we cannot read.

    py -3 update_unsupported.py [--answers gap_urls_dir_or_glob ...] [--dry-run]
Sources of new entries: agy gap-URL answers (adapter null) with their evidence URL.
"""
import argparse
import glob
import json
import re
from pathlib import Path

import atomic_json

# --- config ---
HERE = Path(__file__).resolve().parent
UNSUPPORTED = HERE / "data" / "unsupported.json"
CONFIG = HERE / "data" / "config.json"
ALIASES = Path(r"D:\Code\Resume\_Reference\Company_Aliases.json")
DEFAULT_ANSWERS = [r"C:/Users/yazad/AppData/Local/Temp/claude/ff4b1e9acae04064/abfe4774-2719-4d59-93e9-01cb97ad6d4f/scratchpad/gap_urls/g*.json"]
NEW_REASON = "Custom careers site with no supported ATS (checked 2026-11-20); check the site directly"
URL_COLUMN = "url"
# --- end config ---


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").split("|")[0].lower())


def covered_names():
    names = {norm(c["name"]) for c in json.load(open(CONFIG, encoding="utf-8"))["companies"]}
    for group in json.load(open(ALIASES, encoding="utf-8")).get("groups", []):
        if any(norm(n) in names for n in group):
            names |= {norm(n) for n in group}
    return names


def read_answers(patterns):
    rows = []
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            m = re.search(r"\[\s*\{.*\}\s*\]", open(p, encoding="utf-8", errors="ignore").read(), re.S)
            try:
                rows += json.loads(m.group(0))
            except Exception:
                continue
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--answers", nargs="*", default=DEFAULT_ANSWERS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    data = json.load(open(UNSUPPORTED, encoding="utf-8"))
    covered = covered_names()
    kept = [c for c in data["companies"] if norm(c["name"]) not in covered]
    dropped = [c["name"] for c in data["companies"] if norm(c["name"]) in covered]
    have = {norm(c["name"]) for c in kept}
    added = []
    for a in read_answers(args.answers):
        name = (a.get("company") or "").split("|")[0].strip()
        if not name or a.get("adapter") or norm(name) in covered or norm(name) in have:
            continue
        entry = {"name": name, "reason": NEW_REASON}
        if a.get("evidence_url"):
            entry[URL_COLUMN] = a["evidence_url"]
        kept.append(entry); have.add(norm(name)); added.append(name)
    print(f"dropped {len(dropped)} now polled: {dropped}\nadded {len(added)}; total {len(kept)}")
    if not args.dry_run:
        data["companies"] = kept
        atomic_json.write(UNSUPPORTED, data) if hasattr(atomic_json, "write") else json.dump(data, open(UNSUPPORTED, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()

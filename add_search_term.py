#!/usr/bin/env python3
"""Append one keyword to every board that has a `search` list in data/config.json (idempotent).

    py -3 -X utf8 add_search_term.py "spring 2027"
Why: Workday, Oracle and similar boards are queried by keyword, so a term missing from the list is never fetched.
"""
import json
import sys
from pathlib import Path

# ---- config ----
CONFIG = Path(__file__).resolve().parent / "data" / "config.json"
# ---- end config ----


def main():
    term = sys.argv[1].lower()
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    changed = 0
    for c in cfg["companies"]:
        terms = c.get("search")
        if isinstance(terms, list) and term not in [t.lower() for t in terms]:
            terms.append(term)
            changed += 1
    json.dump(cfg, open(CONFIG, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"boards updated: {changed}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fetch named boards from config.json live and print job count, completeness and error for each.

Use it to tell a board that truly fails from one that only timed out in the shard budget.
    py -3 -X utf8 probe_boards.py "Citi" "MetLife"      # names; no args = boards in health.json failed/incomplete
"""
import json
import sys
import time
from pathlib import Path

# ---- config ----
REPO = Path(__file__).resolve().parent
CONFIG = REPO / "data" / "config.json"
HEALTH = REPO / "health.json"
HEALTH_KEYS = ("failed", "incomplete")
# ---- end config ----

sys.path.insert(0, str(REPO))
import job_scout as j  # noqa: E402


def names_from_health():
    companies = json.load(open(HEALTH, encoding="utf-8"))["companies"]
    return [n for k in HEALTH_KEYS for n in companies.get(k, [])]


def main():
    names = sys.argv[1:] or names_from_health()
    by_name = {c["name"]: c for c in json.load(open(CONFIG, encoding="utf-8"))["companies"]}
    for n in names:
        start = time.time()
        _, jobs, complete, err = j.fetch_company(by_name[n])
        count = None if jobs is None else len(jobs)
        print(f"{n:24} jobs={count} complete={complete} err={str(err)[:140]} {time.time() - start:.0f}s", flush=True)


if __name__ == "__main__":
    main()

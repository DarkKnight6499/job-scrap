#!/usr/bin/env python3
"""One-off: fill in `posted` on queue.json entries that predate the field being added to
_process_company(). Re-fetches each company's current live listing and matches queue entries
by id. A posting no longer in the live listing (filled/pulled) has no way to recover its date
and is left as "unknown" - that's a real limitation, not a bug.

Run once from the repo root: py -3 backfill_posted.py
"""
import json
import sys
from pathlib import Path

import atomic_json
from job_scout import FETCH, HOME

REPO_DIR = Path(__file__).resolve().parent


def main():
    cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    queue = json.loads((HOME / "queue.json").read_text(encoding="utf-8"))
    by_company = {c["name"]: c for c in cfg["companies"]}

    missing = [e for e in queue if not e.get("posted")]
    print(f"{len(missing)} of {len(queue)} queue entries have no posted date.")

    fixed, gone, errors = 0, 0, 0
    for name in sorted({e["company"] for e in missing}):
        entries = [e for e in missing if e["company"] == name]
        c = by_company.get(name)
        if not c:
            print(f"  {name}: not in config.json anymore, skipping {len(entries)}")
            continue
        try:
            live, _complete = FETCH[c["ats"]](c)
        except Exception as exc:
            print(f"  {name}: fetch failed ({exc}), skipping {len(entries)}")
            errors += len(entries)
            continue
        live_by_id = {j["id"]: j.get("posted") for j in live}
        # queue entry id is "{company}:{job_id}" - job_id is what live listings key by
        for e in entries:
            job_id = e["id"].split(":", 1)[1]
            posted = live_by_id.get(job_id)
            if posted:
                e["posted"] = posted
                fixed += 1
            else:
                gone += 1
        print(f"  {name}: {sum(1 for e in entries if live_by_id.get(e['id'].split(':', 1)[1]))}/{len(entries)} recovered")

    atomic_json.write(str(HOME / "queue.json"), queue)
    print(f"\nfixed {fixed}, still unknown (delisted or fetch error) {gone + errors}.")


if __name__ == "__main__":
    sys.exit(main())

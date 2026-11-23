"""Author: Yazad Madan. Add one job board found by hand to data/config.json after a live check (2026-11-23).

For companies discover_boards.py cannot detect (bot-checked career page, ATS link read from a job post): the board is fetched through
the same fetcher the scout uses, must return jobs, and only then is it appended (config keeps indent=1, UTF-8) and written to the tried-log.

Run: py -3 -X utf8 add_board.py "Company Name" --ats adp_rm --set domain=constellationinsurancecareers [--set key=value ...] [--alias "Other Name"] [--note "how it was found"] [--dry-run]
Needs the fields the ats' adapter reads (see config_entry in discover_boards.py and the adapter docstring in job_scout.py). Read the printed titles: they must belong to the company.
After a real add run validate_config.py (done here) and commit config.json plus board_discovery_log.json together.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "data" / "config.json"
LOG_FILE = HERE / "data" / "board_discovery_log.json"
SHOW_TITLES = 10


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def add_board(config_path, log_path, name, ats, fields, fetch, today, aliases=(), note="", dry_run=False):
    """Verify, then append. Returns (ok, message, jobs). `fetch` is job_scout.FETCH[ats]-shaped: entry -> (jobs, complete)."""
    raw = Path(config_path).read_text(encoding="utf-8")
    cfg = json.loads(raw)
    companies = cfg["companies"]
    if any(norm(c["name"]) == norm(name) for c in companies):
        return False, f"{name} is already in config.json", []
    entry = {"name": name, "ats": ats, **fields}
    clash = [c["name"] for c in companies if c.get("ats") == ats and all(c.get(k) == v for k, v in fields.items())]
    if clash:
        return False, f"same board already used by {clash[0]}", []
    try:
        jobs, _complete = fetch(entry)
    except Exception as exc:  # noqa: BLE001
        return False, f"fetch failed: {exc}", []
    if not jobs:
        return False, "the board returned no jobs; not added", []
    if dry_run:
        return True, f"dry run: {len(jobs)} jobs, nothing written", jobs
    companies.append(entry)
    Path(config_path).write_text(json.dumps(cfg, indent=1, ensure_ascii=False) + ("\n" if raw.endswith("\n") else ""), encoding="utf-8", newline="")
    log = json.loads(Path(log_path).read_text(encoding="utf-8")) if Path(log_path).exists() else {}
    found = f"{ats} {next(iter(fields.values()), '')}".strip()
    for label in (name, *aliases):
        log[norm(label)] = {"name": label, "date": today, "found": found, "note": note or f"added by hand, {len(jobs)} jobs verified {today}"}
    Path(log_path).write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8", newline="")
    return True, f"added {name} ({len(jobs)} jobs)", jobs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("--ats", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="key=value", help="adapter field, repeatable")
    ap.add_argument("--alias", action="append", default=[], help="other company name to record in the tried-log")
    ap.add_argument("--note", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    fields = dict(kv.split("=", 1) for kv in a.set)
    sys.path.insert(0, str(HERE))
    import job_scout  # noqa: E402
    if a.ats not in job_scout.FETCH:
        sys.exit(f"unknown ats {a.ats!r}; known: {', '.join(sorted(job_scout.FETCH))}")
    ok, msg, jobs = add_board(CONFIG, LOG_FILE, a.name, a.ats, fields, job_scout.FETCH[a.ats], datetime.date.today().isoformat(), a.alias, a.note, a.dry_run)
    print(msg)
    for j in jobs[:SHOW_TITLES]:
        print("  ", j.get("title"), "|", j.get("location"))
    if not ok:
        sys.exit(1)
    if not a.dry_run:
        done = subprocess.run([sys.executable, "-X", "utf8", str(HERE / "validate_config.py")], capture_output=True, text=True, encoding="utf-8")
        print((done.stdout or done.stderr).strip())
        sys.exit(done.returncode)


if __name__ == "__main__":
    main()

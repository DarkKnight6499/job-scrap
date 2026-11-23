#!/usr/bin/env python3
"""List employer names (one per line in a file or argv) that have no board in data/config.json.

    py -3 -X utf8 missing_boards.py names.txt
Why: after an Indeed or ZipRecruiter pass, shows which employers the scout cannot see, to feed note_missing_company.py and board-discovery.
"""
import json
import re
import sys
from pathlib import Path

# ---- config ----
CONFIG = Path(__file__).resolve().parent / "data" / "config.json"
MIN_NAME_LEN = 3
# ---- end config ----


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def main():
    arg = sys.argv[1:]
    names = Path(arg[0]).read_text(encoding="utf-8").splitlines() if len(arg) == 1 and Path(arg[0]).is_file() else arg
    boards = [norm(c["name"]) for c in json.load(open(CONFIG, encoding="utf-8"))["companies"]]
    for name in filter(None, (n.strip() for n in names)):
        n = norm(name)
        if not any(n in b or (len(b) >= MIN_NAME_LEN and b in n) for b in boards):
            print(name, flush=True)


if __name__ == "__main__":
    main()

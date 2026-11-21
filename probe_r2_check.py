"""Check boards added since git HEAD with FETCH; --apply drops entries that error, return no jobs or duplicate another board."""
import json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "D:/Code/job-scrap")
import job_scout
# ---- config ----
CONFIG = "D:/Code/job-scrap/data/config.json"
APPLY = "--apply" in sys.argv
# ---- end config ----
head = json.loads(subprocess.check_output(["git", "-C", "D:/Code/job-scrap", "show", "HEAD:data/config.json"]).decode("utf-8"))
old = {c["name"] for c in head["companies"]}
cfg = json.load(open(CONFIG, encoding="utf-8"))
new = [c for c in cfg["companies"] if c["name"] not in old]
print("new", len(new), flush=True)


def check(c):
    try:
        jobs, _ = job_scout.FETCH[c["ats"]](c)
        return c["name"], len(jobs), {j["id"] for j in jobs}
    except Exception as e:
        return c["name"], -1, type(e).__name__


bad, seen = set(), {}
with ThreadPoolExecutor(6) as pool:
    for name, n, ids in pool.map(check, new):
        dup = next((o for o, s in seen.items() if isinstance(ids, set) and ids and ids == s), None)
        print(name, n, "DUP of " + dup if dup else "", flush=True)
        if n <= 0 or dup:
            bad.add(name)
        if isinstance(ids, set):
            seen[name] = ids
print("BAD", sorted(bad))
if APPLY:
    cfg["companies"] = [c for c in cfg["companies"] if c["name"] not in bad]
    json.dump(cfg, open(CONFIG, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("removed", len(bad))

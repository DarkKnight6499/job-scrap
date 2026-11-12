#!/usr/bin/env python3
"""Discover public job boards for company names, so LinkedIn scraping is needed less.

Two probes per company, run in parallel:
  1. Career-page scan: fetch likely company domains and careers paths, regex the HTML for ATS links
     (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, BambooHR, Breezy, Workday, iCIMS, Jobvite, iSolved Hire).
  2. Slug guess: call the public JSON API of Greenhouse, Lever, Ashby, SmartRecruiters, Workable, BambooHR.
A hit needs live jobs. Greenhouse names are checked against the company; other APIs return no
company name, so those rows print "verify".

    py -3 discover_boards.py "Company A" "Company B" [--domain "Company A=a.com"]
    py -3 discover_boards.py --from-linkedin          # companies in data/linkedin_candidates.json not in config
    py -3 discover_boards.py --from-file names.txt    # one name per line
    add --write to append hits to data/config.json (indent=1, UTF-8)
Companies on Taleo, UKG, ADP or custom sites print "no board found"; check them by hand.
"""
import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --- config ---
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "data" / "config.json"
LINKEDIN_FILE = HERE / "data" / "linkedin_candidates.json"
LOG_FILE = HERE / "data" / "board_discovery_log.json"  # companies already tried, so later sessions skip them
RETRY_AFTER_DAYS = 30
WORKERS = 6
TIMEOUT_S = 12
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "group", "holdings", "the", "na", "plc", "lp"}
TLDS = ["com", "io", "co", "ai"]
CAREER_PATHS = ["", "/careers", "/jobs", "/company/careers", "/about/careers"]
API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
    "lever": "https://api.lever.co/v0/postings/{s}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{s}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{s}/postings",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{s}",
    "bamboohr": "https://{s}.bamboohr.com/careers/list",
    "breezy": "https://{s}.breezy.hr/json",
}
LINK_RE = {
    "greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)",
    "lever": r"jobs\.lever\.co/([a-z0-9_-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)",
    "smartrecruiters": r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
    "workable": r"apply\.workable\.com/([a-z0-9_-]+)",
    "bamboohr": r"([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)",
    "breezy": r"([a-z0-9-]+)\.breezy\.hr",
    "jobvite": r"jobs\.jobvite\.com/([a-z0-9_-]+)",
    "isolvedhire": r"([a-z0-9-]+\.(?:isolvedhire|applicantpro)\.com)",
    "silkroad": r"jobs\.silkroad\.com/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)",
    "jobaps": r"jobapscloud\.com/([A-Za-z0-9]{2,12})/",
    "applicantstack": r"([a-z0-9-]+\.applicantstack\.com)",
}
# Boards that are not a JSON slug API: their own key field in config, and a live-jobs check
KEY_FIELD = {"icims": "host", "jobvite": "company", "isolvedhire": "host", "silkroad": "portal", "jobaps": "agency",
             "applicantstack": "host"}
WORKDAY_RE = r"https?://([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
ICIMS_RE = r"https?://([a-z0-9-]+\.icims\.com)"
GENERIC_SLUGS = {"embed", "jobs", "careers", "api", "v1", "company", "www"}
# --- end config ---


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def get(url):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT_S).read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return ""


def words(name):
    ws = re.findall(r"[a-z0-9]+", name.lower())
    return [w for w in ws if w not in SUFFIXES] or ws


def slug_guesses(name):
    core, allw = words(name), re.findall(r"[a-z0-9]+", name.lower())
    out = ["".join(core), "-".join(core), "".join(allw), "-".join(allw)]
    return list(dict.fromkeys(s for s in out if s))


def icims_count(host):
    return get(f"https://{host}/jobs/search?ss=1&in_iframe=1&pr=0").count('class="iCIMS_JobCardItem"')


def jobvite_count(slug):
    return get(f"https://jobs.jobvite.com/{slug}/jobs").count('class="jv-job-list-name"')


def isolved_count(host):
    m = re.search(r"domainId\s*:\s*(\d+)", get(f"https://{host}/jobs/"))
    if not m:
        return 0
    try:
        return len((json.loads(get(f"https://{host}/core/jobs/{m.group(1)}?getParams=%7B%7D")).get("data") or {}).get("jobs") or [])
    except ValueError:
        return 0


def silkroad_count(portal):
    return get(f"https://jobs.silkroad.com/{portal}").count('class="sr-panel"')


def jobaps_count(agency):
    return get(f"https://www.jobapscloud.com/{agency}/").count('class="JobNum"')


def applicantstack_count(host):
    return get(f"https://{host}/x/openings").count("/x/detail/")


SPECIAL_COUNT = {"icims": icims_count, "jobvite": jobvite_count, "isolvedhire": isolved_count,
                 "silkroad": silkroad_count, "jobaps": jobaps_count, "applicantstack": applicantstack_count}


def count_jobs(ats, body):
    if not body:
        return 0
    try:
        d = json.loads(body)
    except ValueError:
        return 0
    if ats == "bamboohr":
        return len(d.get("result", [])) if isinstance(d, dict) else 0
    if ats in ("lever", "breezy"):
        return len(d) if isinstance(d, list) else 0
    if ats in ("greenhouse", "ashby", "workable"):
        return len(d.get("jobs", [])) if isinstance(d, dict) else 0
    return int(d.get("totalFound", 0)) if isinstance(d, dict) else 0


def gh_name_ok(slug, name):
    try:
        board = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")).get("name", "")
    except ValueError:
        return None
    a, b = norm(board), norm(" ".join(words(name)))
    return bool(a and b and (a in b or b in a))


def scan_pages(name, domain):
    hosts = [domain] if domain else [f"{''.join(words(name))}.{t}" for t in TLDS]
    for h in hosts:
        for base in (f"https://www.{h}", f"https://{h}"):
            reached = False
            for path in CAREER_PATHS:
                html = get(base + path)
                if not html:
                    continue
                reached = True
                for ats, pat in LINK_RE.items():
                    for m in re.finditer(pat, html):
                        if m.group(1).lower() not in GENERIC_SLUGS:
                            yield ats, m.group(1), f"career page {base + path}"
                for host in dict.fromkeys(re.findall(ICIMS_RE, html)):
                    if not host.startswith(("www.", "api.", "static")):
                        yield "icims", host, f"career page {base + path}"
                m = re.search(WORKDAY_RE, html)
                if m:
                    yield "workday", f"{m.group(1)}|{m.group(2)}", f"career page {base + path}"
            if reached:
                return


def verdict(ats, slug, name):
    if ats != "greenhouse":
        return "verify"
    ok = gh_name_ok(slug, name)
    return "name match" if ok else ("NAME MISMATCH" if ok is False else "verify")


def probe(args):
    name, domain = args
    tried = set()
    for ats, slug, via in scan_pages(name, domain):
        if (ats, slug) in tried:
            continue
        tried.add((ats, slug))
        if ats == "workday":
            host, site = slug.split("|")
            return {"name": name, "ats": "workday", "host": host, "tenant": host.split(".")[0], "site": site,
                    "via": via, "jobs": "?", "check": "verify"}
        if ats in SPECIAL_COUNT:
            n = SPECIAL_COUNT[ats](slug)
            if n:
                return {"name": name, "ats": ats, KEY_FIELD[ats]: slug, "key": slug, "jobs": n, "via": via, "check": "verify"}
            continue
        n = count_jobs(ats, get(API[ats].format(s=slug)))
        if n:
            return {"name": name, "ats": ats, "slug": slug, "jobs": n, "via": via, "check": verdict(ats, slug, name)}
    for s in slug_guesses(name):
        for ats in API:
            n = count_jobs(ats, get(API[ats].format(s=s)))
            if n:
                return {"name": name, "ats": ats, "slug": s, "jobs": n, "via": "slug guess", "check": verdict(ats, s, name)}
    return None


def already_polled(r, cfg):
    """Same board under another company name (Ally vs Ally Financial) must not be added twice."""
    for c in cfg["companies"]:
        if r["ats"] in KEY_FIELD:
            f = KEY_FIELD[r["ats"]]
            if c.get("ats") == r["ats"] and r["key"] in (c.get("hosts") or [c.get(f)]):
                return c["name"]
            continue
        if r["ats"] == "workday" and c.get("ats") == "workday" and c.get("host") == r["host"] and c.get("site") == r["site"]:
            return c["name"]
        if r["ats"] != "workday" and c.get("ats") == r["ats"] and str(c.get("slug", "")).lower() == r["slug"].lower():
            return c["name"]
    return None


def config_entry(r, cfg):
    if r["ats"] == "workday":
        tmpl = next(c for c in cfg["companies"] if c.get("ats") == "workday")
        return {"name": r["name"], "ats": "workday", "host": r["host"], "tenant": r["tenant"], "site": r["site"], "search": tmpl["search"]}
    if r["ats"] == "icims":
        return {"name": r["name"], "ats": "icims", "hosts": [r["host"]]}
    if r["ats"] in KEY_FIELD:
        return {"name": r["name"], "ats": r["ats"], KEY_FIELD[r["ats"]]: r["key"]}
    return {"name": r["name"], "ats": r["ats"], "slug": r["slug"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--domain", action="append", default=[], help='"Company=domain.com" (repeatable)')
    ap.add_argument("--from-linkedin", action="store_true")
    ap.add_argument("--from-file")
    ap.add_argument("--retry", action="store_true", help="re-probe companies logged as tried within the retry window")
    ap.add_argument("--write", action="store_true", help="append hits to data/config.json (skips NAME MISMATCH and Workday)")
    ap.add_argument("--workday", action="store_true", help="with --write, also write Workday hits (site name needs a manual check)")
    a = ap.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    have = {norm(c["name"]) for c in cfg["companies"]}
    doms = dict(d.split("=", 1) for d in a.domain)
    names = list(a.names)
    if a.from_file:
        names += [x.strip() for x in Path(a.from_file).read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.from_linkedin:
        cnt = {}
        for r in json.loads(LINKEDIN_FILE.read_text(encoding="utf-8")):
            cnt[r["company"]] = cnt.get(r["company"], 0) + 1
        names += [n for n, _ in sorted(cnt.items(), key=lambda x: -x[1])]
    log = json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else {}
    today = datetime.date.today()

    def recently_tried(n):
        e = log.get(norm(n))
        if not e:
            return False
        return (today - datetime.date.fromisoformat(e["date"])).days < RETRY_AFTER_DAYS

    todo = [n for n in dict.fromkeys(names) if norm(n) not in have and (a.retry or not recently_tried(n))]
    print(f"{len(todo)} to probe, {len(set(names)) - len(todo)} skipped (in config, tried within {RETRY_AFTER_DAYS} days)", file=sys.stderr)
    add = []

    def report(n, r):
        log[norm(n)] = {"name": n, "date": today.isoformat(), "found": f"{r['ats']} {r.get('slug') or r.get('site')}" if r else None}
        if not r:
            print(f"{n}: no board found (check Taleo/UKG/ADP/custom by hand)", flush=True)
            return
        dup = already_polled(r, cfg)
        if dup:
            print(f"{n}: board already polled as '{dup}'", flush=True)
            return
        loc = r.get("slug") or r.get("key") or f"{r['host']}/{r['site']}"
        print(f"{n}: {r['ats']} {loc} | jobs {r['jobs']} | {r['check']} | via {r['via']}", flush=True)
        if r["check"] != "NAME MISMATCH" and (r["ats"] != "workday" or a.workday):
            add.append(config_entry(r, cfg))

    # Stream each result as it completes and save the tried-log every 10, so a long run is readable and resumable
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(probe, (n, doms.get(n))): n for n in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            report(futs[fut], fut.result())
            if i % 10 == 0:
                LOG_FILE.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8", newline="")
    LOG_FILE.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8", newline="")
    if a.write and add:
        cfg["companies"] += add
        CONFIG.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8", newline="")
        print(f"wrote {len(add)} to config ({len(cfg['companies'])} total). Review 'verify' rows before the next scout run.")


if __name__ == "__main__":
    main()

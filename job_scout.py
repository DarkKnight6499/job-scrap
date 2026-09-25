#!/usr/bin/env python3
"""
job_scout.py - free job discovery. Polls public ATS job APIs (Greenhouse,
Lever, Ashby, SmartRecruiters, Workday, Oracle Recruiting Cloud) for a list
of target companies - no paid scraping API, no LLM tokens.

Standalone version: unlike the private-repo original, this build has no
dependency on an application tracker. It does NOT know what you've already
applied to, so it will alert again on a posting if it drops out of a
company's search-result window and reappears later. That's the trade-off
for being runnable from a public repo with no PII.

For every NEW role that matches the title/location filters it:
  1. fetches the full description and classifies sponsorship (Blocked /
     Unclear - never "Allowed", since absence of blocking language proves
     nothing either way);
  2. appends it to the triage queue and sends a phone alert (ntfy) unless
     it was auto-blocked on sponsorship.

Usage:
    python job_scout.py --init              create data/config.json
    python job_scout.py --check             per-company job/match counts, no state change
    python job_scout.py --dry-run           print what would be queued/sent, no state change
    python job_scout.py                     normal run (schedule this)
    python job_scout.py --queue [--all] [--json]   show the triage queue (default: state=new only)
    python job_scout.py --mark <id> <new|shortlisted|applied|dismissed>

Runtime files live in ./data/: config.json (companies/filters - the ntfy
topic is NOT stored here, see notify() below), state.json, queue.json.
Override the data dir with JOB_SCOUT_HOME.
"""
import argparse
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR))

import atomic_json  # noqa: E402
from keyword_matching import contains_term  # noqa: E402

HOME = Path(os.environ.get("JOB_SCOUT_HOME", REPO_DIR / "data"))
UA = {"User-Agent": "job-scout/1.0", "Accept": "application/json"}
QUEUE_STATES = ("new", "shortlisted", "applied", "dismissed", "auto_blocked")

BLOCK_PATTERNS = [
    r"(?:will|would|does|do|can|could|is|are)\s+not\s+(?:\w+\s+){0,3}sponsor",
    r"\bcannot\s+(?:\w+\s+){0,3}sponsor",
    r"unable\s+to\s+(?:\w+\s+){0,3}sponsor",
    r"\bno\s+(?:work\s+)?(?:visa\s+)?sponsorship",
    r"visa\s+sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"sponsor[^.]{0,120}(?:now|currently)[^.]{0,20}(?:or|and)\s+in\s+the\s+future",
    r"(?:now|currently)[^.]{0,20}(?:or|and)\s+in\s+the\s+future[^.]{0,120}sponsor",
    r"not\s+eligible\s+for[^.]{0,60}(?:h-?1b|employer.sponsored)",
    r"\bITAR\b",
    r"\b(?:active|current)\s+(?:top\s+secret|secret|ts/sci)\b",
    r"must\s+be\s+a\s+u\.?s\.?\s+citizen",
    r"u\.?s\.?\s+citizens?\s+(?:only|required)",
    r"(?:u\.?s\.?\s+citizenship|permanent\s+residen(?:t|cy))\s+(?:is\s+)?required",
]
BLOCK_RES = [re.compile(p, re.I) for p in BLOCK_PATTERNS]


# ---------------------------------------------------------------- http / text

def http(url, data=None, headers=None, timeout=20, raw=False):
    h = dict(UA)
    body = None
    if data is not None and not raw:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    elif raw:
        body = data
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = r.read()
    return payload if raw else json.loads(payload)


def strip_html(text):
    text = html.unescape(text or "")
    text = html.unescape(text)  # Greenhouse double-escapes
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------- fetchers

def greenhouse(c):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    return [dict(id=str(j["id"]), title=j["title"], location=(j.get("location") or {}).get("name", ""),
                 url=j["absolute_url"], posted=(j.get("first_published") or "")[:10]) for j in d.get("jobs", [])]


def lever(c):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    d = http(f"https://{host}/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in d:
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for lst in j.get("lists") or []:
            desc += " " + (lst.get("text") or "") + " " + strip_html(lst.get("content", ""))
        desc += " " + (j.get("additionalPlain") or "")
        posted = ""
        ms = j.get("createdAt")
        if isinstance(ms, (int, float)):
            posted = date.fromtimestamp(ms / 1000).isoformat()
        out.append(dict(id=j["id"], title=j["text"], location=(j.get("categories") or {}).get("location", "") or "",
                        url=j["hostedUrl"], desc=desc, posted=posted))
    return out


def ashby(c):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
    return [dict(id=j["id"], title=j["title"], location=j.get("location", "") or "", url=j.get("jobUrl", ""),
                 desc=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                 posted=(j.get("publishedAt") or "")[:10]) for j in d.get("jobs", [])]


def smartrecruiters(c):
    out, offset = [], 0
    while offset < 2000:
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100&offset={offset}")
        rows = d.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            out.append(dict(id=j["id"], title=j["name"],
                            location=", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                            url=f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                            posted=(j.get("releasedDate") or "")[:10]))
        offset += 100
        if len(rows) < 100:
            break
    return out


_RELATIVE_DAYS_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s*day)", re.I)


def _workday_posted(text):
    """Workday's postedOn is relative text ("Posted 5 Days Ago", "Posted Today", "Posted 30+ Days
    Ago") not a real date - approximate it as an ISO date so postings can be sorted/filtered by
    recency. "30+" is a floor, not exact, since Workday caps the display at that bucket."""
    m = _RELATIVE_DAYS_RE.search(text or "")
    if not m:
        return ""
    if m.group(1).lower() == "today":
        days = 0
    elif m.group(1).lower() == "yesterday":
        days = 1
    else:
        days = int(m.group(2))
    return (date.today() - timedelta(days=days)).isoformat()


def workday(c):
    """Unofficial endpoint. Needs host, tenant, site. 'search' is a term or a list of terms; each
    term is queried separately (max_per_term results, default 60) and results are merged by id."""
    base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    out, seen = [], set()
    page = 20  # Workday's unofficial endpoint 400s on limit > 20 - confirmed by testing, not documented
    for term in terms:
        offset = 0
        while offset < cap:
            d = http(base, data={"appliedFacets": {}, "limit": page, "offset": offset, "searchText": term})
            rows = d.get("jobPostings", [])
            for j in rows:
                path = j.get("externalPath")
                if not path or path in seen:  # some rows are placeholder cards with no title/path
                    continue
                seen.add(path)
                out.append(dict(id=path, title=j["title"], location=j.get("locationsText", "") or "",
                                url=f"https://{c['host']}/{c['site']}{path}", posted=_workday_posted(j.get("postedOn"))))
            offset += page
            if len(rows) < page:
                break
    return out


def oracle(c):
    """Oracle Recruiting Cloud (Fusion HCM) - unofficial/undocumented endpoint. Needs host, site
    (Oracle's siteNumber, e.g. "CX_1001"). Same per-term paged search shape as workday(), but
    Oracle's quirks (reverse-engineered against a real tenant, not in any doc):
      - the search term goes in finder as "keyword=", not a top-level searchText param;
      - limit/offset MUST live inside the finder string too - Oracle silently ignores them as
        top-level query params and always returns a fixed first page of 25;
      - expand=requisitionList is required or the response has only facet metadata, no jobs."""
    base = f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    page = 50
    out, seen = [], set()
    for term in terms:
        offset = 0
        while offset < cap:
            finder = f"findReqs;siteNumber={c['site']},limit={page},offset={offset}"
            if term:
                finder += f",keyword={term}"
            url = base + "?" + urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList", "finder": finder})
            d = http(url)
            items = d.get("items") or [{}]
            rows = items[0].get("requisitionList") or []
            for j in rows:
                jid = str(j.get("Id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append(dict(id=jid, title=j.get("Title", "") or "", location=j.get("PrimaryLocation", "") or "",
                                url=f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/job/{jid}",
                                posted=(j.get("PostedDate") or "")[:10]))
            offset += page
            if len(rows) < page:
                break
    return out


FETCH = dict(greenhouse=greenhouse, lever=lever, ashby=ashby, smartrecruiters=smartrecruiters, workday=workday,
             oracle=oracle)
FETCH_WORKERS = 12  # fetches are I/O-bound (network wait); parallelizing across companies cuts wall-clock a lot


def fetch_company(c):
    """Runs on a worker thread. Retries once after a 5s sleep on any error - absorbs a scheduled
    run firing right as the runner's network isn't fully up yet."""
    try:
        return c["name"], FETCH[c["ats"]](c), None
    except Exception:
        time.sleep(5)
        try:
            return c["name"], FETCH[c["ats"]](c), None
        except Exception as e2:
            return c["name"], None, e2


def describe(c, job):
    """Full description text for one job. Lever/Ashby already carry it from the list call."""
    if job.get("desc"):
        return job["desc"]
    ats = c["ats"]
    if ats == "greenhouse":
        d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs/{job['id']}")
        return strip_html(d.get("content", ""))
    if ats == "smartrecruiters":
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{job['id']}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return strip_html(" ".join((s or {}).get("text", "") for s in secs.values()))
    if ats == "workday":
        d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}{job['id']}")
        return strip_html((d.get("jobPostingInfo") or {}).get("jobDescription", ""))
    if ats == "oracle":
        finder = f"ById;Id={job['id']}"
        url = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?"
               + urllib.parse.urlencode({"onlyData": "true", "finder": finder}))
        d = http(url)
        items = d.get("items") or []
        if not items:
            return ""
        it = items[0]
        parts = [it.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
        return strip_html(" ".join(p for p in parts if p))
    return ""


# ------------------------------------------------------------------ analysis

def matches(job, f):
    # Title include/exclude use word-boundary matching (keyword_matching.contains_term) so a short
    # term like "intern" doesn't false-positive inside "Internal"/"International". Locations stay
    # plain substring: filters like ", nj" rely on punctuation that word-boundary matching would
    # reject (the comma has no alnum neighbor to anchor against).
    t, loc = job["title"], job["location"].lower()
    inc = f.get("title_include", [])
    exc = f.get("title_exclude", [])
    locs = [x.lower() for x in f.get("locations_include", [])]
    if inc and not any(contains_term(t, x) for x in inc):
        return False
    if any(contains_term(t, x) for x in exc):
        return False
    if locs and not any(x in loc for x in locs):
        return False
    return True


def classify_sponsorship(text):
    """Blocked on explicit language, else Unclear. Never Allowed."""
    if not text:
        return "Unclear", "Description unavailable; not checked."
    for rx in BLOCK_RES:
        m = rx.search(text)
        if m:
            s, e = max(0, m.start() - 50), min(len(text), m.end() + 50)
            return "Blocked", "..." + text[s:e].strip() + "..."
    return "Unclear", "No sponsorship language in the description."


def keyword_hits(title, text, keywords):
    hay = f"{title} {text}".lower()
    return [k for k in keywords if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", hay)]


# --------------------------------------------------------------------- notify

def notify(cfg, entry, dry):
    line = (f"{entry['company']}: {entry['role']} ({entry['location'] or 'n/a'}) | "
            f"sponsorship {entry['sponsorship_status']}"
            + (f" | {', '.join(entry['kw_hits'][:5])}" if entry["kw_hits"] else ""))
    kind = cfg.get("type", "stdout")
    if dry or kind == "stdout":
        print(("[dry-run] " if dry else "[alert] ") + line + "\n    " + entry["link"])
        return
    topic = cfg.get("topic") or os.environ.get("NTFY_TOPIC")
    if kind == "ntfy" and not topic:
        print(f"! notify skipped: no ntfy topic (set NTFY_TOPIC env var or cfg.notify.topic): {line}", file=sys.stderr)
        return
    try:
        if kind == "ntfy":
            server = cfg.get("server", "https://ntfy.sh").rstrip("/")
            hdr = {"Title": f"New job: {entry['company']}".encode("ascii", "ignore").decode(), "Click": entry["link"]}
            http(f"{server}/{topic}", data=line.encode(), headers=hdr, raw=True)
        elif kind == "telegram":
            http(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                 data={"chat_id": cfg["chat_id"], "text": f"{line}\n{entry['link']}"})
    except Exception as e:  # an alert failure must not lose the queue entry
        print(f"! notify failed: {e}", file=sys.stderr)


# ------------------------------------------------------------------- commands

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def cmd_init():
    HOME.mkdir(parents=True, exist_ok=True)
    cfg_path = HOME / "config.json"
    if cfg_path.exists():
        print(f"{cfg_path} already exists; not overwriting.")
        return
    example = REPO_DIR / "config.example.json"
    cfg = json.loads(example.read_text(encoding="utf-8"))
    atomic_json.write(str(cfg_path), cfg)
    print(f"Wrote {cfg_path}\nSet the NTFY_TOPIC env var (or GitHub Actions secret) to your ntfy.sh topic - "
          f"it is deliberately not stored in config.json since this repo is public.")


def cmd_queue(args):
    q = load_json(HOME / "queue.json", [])
    if not args.all:
        q = [e for e in q if e["state"] == "new"]
    if args.days is not None:
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()
        # postings with no parseable date are kept, not dropped - "unknown" isn't the same as "old"
        q = [e for e in q if not e.get("posted") or e["posted"] >= cutoff]
    def sort_key(e):
        try:
            ordinal = date.fromisoformat(e.get("posted") or "").toordinal()
        except ValueError:
            ordinal = -1  # unknown posting date: sort last, not "recent"
        return (-ordinal, -len(e["kw_hits"]), e["company"])

    q = sorted(q, key=sort_key)
    if args.json:
        print(json.dumps(q, indent=1))
        return
    if args.html:
        write_queue_html(q, args.html)
        print(f"wrote {len(q)} entries to {args.html}")
        return
    for e in q:
        print(f"{e['id']}  {e['company']}: {e['role']} ({e['location']}) posted {e.get('posted') or 'unknown'} "
              f"[{e['state']}, spons {e['sponsorship_status']}, kw {len(e['kw_hits'])}]\n    {e['link']}")
    print(f"{len(q)} queue entr{'y' if len(q) == 1 else 'ies'}.")


def write_queue_html(q, path):
    """Static page, no server: q is already sorted newest-posted-first by cmd_queue. Each row is a
    plain <a> to the live posting so double-clicking the file and clicking a link is the whole workflow."""
    rows = []
    today = date.today().isoformat()
    for e in q:
        posted = e.get("posted") or ""
        posted_label = "today" if posted == today else (posted or "unknown")
        badge = " today" if posted == today else ""
        rows.append(
            f'<tr class="{e["state"]}{badge}">'
            f'<td class="posted">{html.escape(posted_label)}</td>'
            f'<td>{html.escape(e["company"])}</td>'
            f'<td><a href="{html.escape(e["link"])}" target="_blank" rel="noopener">{html.escape(e["role"])}</a></td>'
            f'<td>{html.escape(e["location"])}</td>'
            f'<td>{html.escape(e["sponsorship_status"])}</td>'
            f'<td>{len(e["kw_hits"])}</td>'
            f'<td>{html.escape(e["state"])}</td>'
            f'</tr>'
        )
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Job Scout Queue</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; color: #111; }}
h1 {{ font-size: 1.2rem; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: left; font-size: 0.9rem; }}
th {{ position: sticky; top: 0; background: #fafafa; cursor: pointer; }}
tr.today {{ background: #eaffea; font-weight: 600; }}
tr.auto_blocked {{ color: #999; }}
td.posted {{ white-space: nowrap; }}
</style></head>
<body>
<h1>Job Scout Queue - generated {date.today().isoformat()} - {len(q)} entries</h1>
<table id="q">
<thead><tr><th>Posted</th><th>Company</th><th>Role</th><th>Location</th><th>Sponsorship</th><th>Kw</th><th>State</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</body></html>"""
    Path(path).write_text(page, encoding="utf-8")


def cmd_mark(args):
    q = load_json(HOME / "queue.json", [])
    if args.state not in QUEUE_STATES:
        sys.exit(f"state must be one of {QUEUE_STATES}")
    hit = [e for e in q if e["id"] == args.id]
    if not hit:
        sys.exit(f"no queue entry {args.id}")
    hit[0]["state"] = args.state
    atomic_json.write(str(HOME / "queue.json"), q)
    print(f"{args.id} -> {args.state}")


def run(args):
    cfg = load_json(HOME / "config.json", None)
    if cfg is None:
        sys.exit(f"No config at {HOME / 'config.json'}. Run: python job_scout.py --init")
    filters = cfg.get("filters", {})
    keywords = cfg.get("keywords", [])
    state = load_json(HOME / "state.json", {})
    queue = load_json(HOME / "queue.json", [])
    queued_ids = {e["id"] for e in queue}
    run_id = "scout_" + date.today().isoformat()
    counts = dict(companies=0, errors=0, matched=0, blocked=0, queued=0, alerted=0)

    fetched = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(fetch_company, c): c for c in cfg["companies"]}
        for fut in as_completed(futures):
            name, jobs, err = fut.result()
            fetched[name] = (jobs, err)

    # One shared pool for every description fetch across every company, not one pool per company -
    # several companies finishing their listing fetch around the same time and each spinning up
    # their own pool caused a brief concurrency spike that tripped Workday's rate limiting (429s).
    describe_pool = ThreadPoolExecutor(max_workers=8)
    try:
        for c in cfg["companies"]:
            name = c["name"]
            jobs, err = fetched[name]
            if err is not None:
                counts["errors"] += 1
                print(f"! {name} ({c['ats']}): {err}", file=sys.stderr)
                continue
            counts["companies"] += 1
            hits = [j for j in jobs if matches(j, filters)]
            if args.check:
                warn = "  <- 0 jobs, check slug" if not jobs else ""
                print(f"ok {name}: {len(jobs)} jobs, {len(hits)} match{warn}")
                continue

            seen = set(state.get(name, []))
            first_run = name not in state
            fresh = [j for j in (hits if first_run else [j for j in hits if j["id"] not in seen])
                     if f"{name}:{j['id']}" not in queued_ids]
            descriptions = {}
            if fresh:
                futures = {describe_pool.submit(describe, c, j): j for j in fresh}
                for fut in as_completed(futures):
                    j = futures[fut]
                    try:
                        descriptions[j["id"]] = fut.result()
                    except Exception as e:
                        print(f"! {name} description for {j['title']}: {e}", file=sys.stderr)
                        descriptions[j["id"]] = ""
            _process_company(c, name, fresh, descriptions, first_run, jobs, seen, state, queue, queued_ids,
                              keywords, cfg, args, counts)
    finally:
        describe_pool.shutdown()

    if args.check:
        return
    if not args.dry_run:
        HOME.mkdir(parents=True, exist_ok=True)
        atomic_json.write(str(HOME / "state.json"), state)
        atomic_json.write(str(HOME / "queue.json"), queue)
    print(f"done: {counts['companies']} companies ({counts['errors']} errors), {counts['matched']} new matches, "
          f"{counts['blocked']} auto-blocked on sponsorship, {counts['queued']} queued, {counts['alerted']} alerts.")


def _process_company(c, name, fresh, descriptions, first_run, jobs, seen, state, queue, queued_ids,
                      keywords, cfg, args, counts):
    for j in fresh:
        counts["matched"] += 1
        eid = f"{name}:{j['id']}"
        text = descriptions.get(j["id"], "")
        spons, evidence = classify_sponsorship(text)
        entry = dict(id=eid, company=name, role=j["title"], location=j["location"], link=j["url"],
                     posted=j.get("posted") or "", source="job-scout", first_seen=date.today().isoformat(),
                     sponsorship_status=spons, sponsorship_evidence=evidence,
                     kw_hits=keyword_hits(j["title"], text, keywords),
                     state="auto_blocked" if spons == "Blocked" else "new", first_run=first_run)
        queue.append(entry)
        queued_ids.add(eid)
        if spons == "Blocked":
            counts["blocked"] += 1
            continue
        counts["queued"] += 1
        if not first_run:  # first run seeds the queue silently; only genuinely new postings alert
            notify(cfg.get("notify", {}), entry, args.dry_run)
            counts["alerted"] += 1
    if not args.dry_run:
        state[name] = sorted(seen | {j["id"] for j in jobs})  # union: a capped search must not re-alert on drift
    elif first_run:
        print(f"[dry-run] {name}: first run, would queue {len(fresh)} current matches silently")


def main():
    ap = argparse.ArgumentParser(description="Free ATS job discovery, standalone (no tracker dedup).")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --queue: include every state")
    ap.add_argument("--json", action="store_true", help="with --queue: JSON output")
    ap.add_argument("--days", type=int, help="with --queue: only postings posted within the last N days")
    ap.add_argument("--html", metavar="PATH", help="with --queue: write a clickable HTML page instead of stdout")
    ap.add_argument("--mark", nargs=2, metavar=("ID", "STATE"))
    args = ap.parse_args()
    if args.init:
        return cmd_init()
    if args.queue:
        return cmd_queue(args)
    if args.mark:
        args.id, args.state = args.mark
        return cmd_mark(args)
    run(args)


if __name__ == "__main__":
    main()

"""Turn probed ATS URLs into config entries and verify each with job_scout.FETCH; stream results."""
import html, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, "D:/Code/job-scrap")
import job_scout
# ---- config ----
SCRATCH = "D:/Code/job-scrap/data/probe/"
IN = SCRATCH + "ats_urls.jsonl"
OUT = SCRATCH + "ats_verified.jsonl"
CONFIG = "D:/Code/job-scrap/data/config.json"
# ---- end config ----


def entry(name, url):
    u = html.unescape(url.replace("&#038;", "&").replace("&amp;", "&"))
    p = urlparse(u)
    host, path, q = p.netloc.lower(), p.path, parse_qs(p.query)
    if host.endswith("ukg.net") or "ultipro.com" in host:
        m = re.match(r"(/[A-Z0-9]+/JobBoard/[0-9a-f-]{36})", path, re.I)
        if m:
            return dict(name=name, ats="ukg", board=f"https://{host}{m.group(1)}")
    if host == "workforcenow.adp.com" and q.get("cid"):
        return dict(name=name, ats="adp_wfn", cid=q["cid"][0])
    if host.endswith("oraclecloud.com"):
        m = re.search(r"/sites/(CX_\w+)", path)
        if m:
            return dict(name=name, ats="oracle", host=host.split(":")[0], site=m.group(1))
    if host.endswith("myworkdayjobs.com"):
        parts = [x for x in path.split("/") if x and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", x)]
        if parts:
            return dict(name=name, ats="workday", host=host, tenant=host.split(".")[0], site=parts[0])
    if host.endswith("lever.co") and path.strip("/"):
        return dict(name=name, ats="lever", slug=path.strip("/").split("/")[0])
    if host.endswith("bamboohr.com") and host.split(".")[0] != "www":
        return dict(name=name, ats="bamboohr", slug=host.split(".")[0])
    if host.endswith("governmentjobs.com") and path.startswith("/careers/"):
        return dict(name=name, ats="neogov", agency=path.split("/")[2])
    if host.endswith("paylocity.com") and "/jobs/" in path:
        return dict(name=name, ats="paylocity", board=f"https://{host}{path}")
    if host.endswith("dayforcehcm.com"):
        m = re.search(r"/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/([^/?]+)", path)
        if m and "candidateportal" in path.lower():
            return dict(name=name, ats="dayforce", namespace=m.group(1), board=path.rstrip("/").split("/")[-1])
        m = re.match(r"/en-US/([^/]+)/([^/?]+)", path)
        if m:
            return dict(name=name, ats="dayforce", namespace=m.group(1), board=m.group(2))
    if host.endswith("csod.com") and host.split(".")[0] not in ("www", "stateofohio"):
        return dict(name=name, ats="csod", corp=host.split(".")[0])
    if host.endswith("taleo.net") and ".tbe." in host:
        m = re.match(r"/(\w+)/", path)
        if m and q.get("org") and q.get("cws"):
            return dict(name=name, ats="taleo_cws", host=host, path=m.group(1), org=q["org"][0], cws=q["cws"][0])
    if host.endswith("greenhouse.io") and q.get("for"):
        return dict(name=name, ats="greenhouse", slug=q["for"][0])
    return None


def verify(c):
    try:
        jobs, complete = job_scout.FETCH[c["ats"]](c)
        return dict(c, jobs=len(jobs), complete=complete, sample=[j["title"] for j in jobs[:2]])
    except Exception as e:
        return dict(c, jobs=-1, error=type(e).__name__ + ": " + str(e)[:80])


existing = {json.dumps({k: v for k, v in c.items() if k not in ("name", "search")}, sort_keys=True) for c in json.load(open(CONFIG, encoding="utf-8"))["companies"]}
cands, seen = [], set()
for line in open(IN, encoding="utf-8"):
    r = json.loads(line)
    for url in r["urls"]:
        c = entry(r["name"], url)
        if not c:
            continue
        key = json.dumps({k: v for k, v in c.items() if k != "name"}, sort_keys=True)
        if key in existing or key in seen:
            continue
        seen.add(key)
        cands.append(c)
        break
print("candidates", len(cands), flush=True)
with ThreadPoolExecutor(6) as pool, open(OUT, "w", encoding="utf-8") as out:
    for res in pool.map(verify, cands):
        out.write(json.dumps(res) + "\n"); out.flush()
        print(res["ats"], res["name"], res["jobs"], res.get("error", ""), res.get("sample", ""), flush=True)
print("VERIFY_DONE", flush=True)

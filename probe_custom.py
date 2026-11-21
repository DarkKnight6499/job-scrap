"""Step 1 of the custom-site probe (then probe_ats_urls.py, probe_ats_verify.py, probe_sm_test.py). Probe each unsupported careers URL for readable job sources, streaming one JSON line per site."""
import json, re, sys, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
# ---- config ----
SRC = "D:/Code/job-scrap/data/unsupported.json"
OUT = "D:/Code/job-scrap/data/probe/probe_custom.jsonl"
WORKERS, TIMEOUT = 10, 15
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
ATS_HOSTS = re.compile(r"(myworkdayjobs\.com|greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|icims\.com|jobvite\.com|taleo\.net|ultipro\.com|ukg\.(?:com|net)|paylocity\.com|paycor\.com|paycomonline\.net|adp\.com|dayforcehcm\.com|oraclecloud\.com|csod\.com|applytojob\.com|applicantpro\.com|isolvedhire\.com|brassring\.com|successfactors\.(?:com|eu)|jobs2web\.com|phenompeople\.com|avature\.net|eightfold\.ai|recruitee\.com|pinpointhq\.com|hire\.trakstar\.com|workable\.com|bamboohr\.com|breezy\.hr|jazz\.co|governmentjobs\.com|neogov\.com|pageuppeople\.com|peoplefluent\.com|atsondemand\.com|hrmdirect\.com|silkroad\.com|recruiting\.ultipro\.com|talemetry\.com|clearcompany\.com|selectminds\.com|oracle\.com/hcm|csod\.com|cornerstoneondemand\.com|ceridian\.com|hirebridge\.com|applicantstack\.com|zohorecruit\.com|freshteam\.com|gem\.com|wd\d+\.myworkdayjobs)", re.I)
# ---- end config ----
def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT).read(600000).decode("utf-8", "ignore")
def probe(c):
    url = c.get("url") or ""
    r = {"name": c["name"], "url": url}
    if not url.startswith("http"):
        return {**r, "status": "no_url"}
    try:
        page = get(url)
    except Exception as e:
        return {**r, "status": "fetch_fail", "error": type(e).__name__}
    r["ats_hosts"] = sorted({m.group(1).lower() for m in ATS_HOSTS.finditer(page)})[:6]
    r["jsonld_job"] = bool(re.search(r'"@type"\s*:\s*"JobPosting"', page))
    r["rss"] = bool(re.search(r'<link[^>]+type="application/(?:rss|atom)\+xml"', page, re.I))
    r["wp_jobs"] = bool(re.search(r"wp-content/plugins/(?:wp-job-manager|job-manager|jobboard|simple-job-board|recruit)", page, re.I))
    r["iframe_src"] = sorted(set(re.findall(r'<iframe[^>]+src="(https?://[^"]+)"', page, re.I)))[:3]
    host = "{u.scheme}://{u.netloc}".format(u=urllib.parse.urlparse(url))
    sm = []
    try:
        for line in get(host + "/robots.txt").splitlines():
            if line.lower().startswith("sitemap:"):
                sm.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    r["sitemaps"] = len(sm)
    r["job_sitemap"] = any(re.search(r"job|career|position|opening|vacan", s, re.I) for s in sm)
    r["status"] = "ok"
    return r
rows = json.load(open(SRC, encoding="utf-8"))["companies"]
rows = [c for c in rows if c.get("url")]
with ThreadPoolExecutor(WORKERS) as pool, open(OUT, "w", encoding="utf-8") as out:
    for f in as_completed([pool.submit(probe, c) for c in rows]):
        res = f.result(); out.write(json.dumps(res) + "\n"); out.flush()
        print(res["status"], res["name"], res.get("ats_hosts", ""), flush=True)
print("PROBE_DONE", len(rows), flush=True)

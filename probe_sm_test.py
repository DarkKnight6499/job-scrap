"""Step 4: test RSS feeds and job sitemaps from probe_custom.jsonl with the sitemap adapter."""
import json, re, sys, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "D:/Code/job-scrap")
import job_scout
# ---- config ----
SCRATCH = "D:/Code/job-scrap/data/probe/"
IN = SCRATCH + "probe_custom.jsonl"
OUT = SCRATCH + "sm_test.jsonl"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36"}
JOBWORD = re.compile(r"job|career|position|opening|vacan", re.I)
# ---- end config ----


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read(600000).decode("utf-8", "ignore")


def test(r):
    cands = []
    try:
        page = get(r["url"])
        for m in re.finditer(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', page, re.I):
            h = re.search(r'href="([^"]+)"', m.group(0))
            if h:
                cands.append(urllib.parse.urljoin(r["url"], h.group(1).replace("&amp;", "&")))
        host = "{u.scheme}://{u.netloc}".format(u=urllib.parse.urlparse(r["url"]))
        for line in get(host + "/robots.txt").splitlines():
            if line.lower().startswith("sitemap:") and JOBWORD.search(line):
                cands.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    res = []
    for u in dict.fromkeys(cands):
        try:
            jobs, complete = job_scout.sitemap({"sitemap": u})
            res.append(dict(name=r["name"], sitemap=u, jobs=len(jobs), sample=[j["title"] for j in jobs[:3]]))
        except Exception as e:
            res.append(dict(name=r["name"], sitemap=u, jobs=-1, error=type(e).__name__))
    return res


rows = [json.loads(l) for l in open(IN, encoding="utf-8")]
rows = [r for r in rows if r["status"] == "ok" and (r["rss"] or r["job_sitemap"])]
print("sites", len(rows), flush=True)
with ThreadPoolExecutor(8) as pool, open(OUT, "w", encoding="utf-8") as out:
    for res in pool.map(test, rows):
        for x in res:
            out.write(json.dumps(x) + "\n"); out.flush()
            if x["jobs"] > 0:
                print(x["name"], x["jobs"], x["sitemap"][:80], x["sample"], flush=True)
print("SM_DONE", flush=True)

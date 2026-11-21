"""Extract concrete ATS tenant URLs from pages whose probe found an ATS host."""
import json, re, urllib.request
from concurrent.futures import ThreadPoolExecutor
# ---- config ----
IN = "D:/Code/job-scrap/data/probe/probe_custom.jsonl"; OUT = "D:/Code/job-scrap/data/probe/ats_urls.jsonl"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36"}
PAT = re.compile(r"https?://[\w.-]*(?:recruiting\.ultipro\.com|ultipro\.com|ukg\.net|icims\.com|oraclecloud\.com|successfactors\.(?:com|eu)|phenompeople\.com|paycomonline\.net|paycor\.com|paylocity\.com|dayforcehcm\.com|csod\.com|taleo\.net|myworkdayjobs\.com|greenhouse\.io|lever\.co|bamboohr\.com|freshteam\.com|workforcenow\.adp\.com|recruitingbypaycor\.com|governmentjobs\.com|pageuppeople\.com)[^\s<>)'\x22]*", re.I)
# ---- end config ----
def one(r):
    try:
        page = urllib.request.urlopen(urllib.request.Request(r["url"], headers=UA), timeout=15).read(600000).decode("utf-8", "ignore")
    except Exception:
        return None
    urls = sorted({m.group(0).rstrip(".,;") for m in PAT.finditer(page)})[:5]
    return {"name": r["name"], "page": r["url"], "urls": urls} if urls else None
rows = [json.loads(l) for l in open(IN, encoding="utf-8")]
rows = [r for r in rows if r["status"] == "ok" and r["ats_hosts"]]
with ThreadPoolExecutor(10) as p, open(OUT, "w", encoding="utf-8") as o:
    for res in p.map(one, rows):
        if res:
            o.write(json.dumps(res) + "\n"); print(res["name"], res["urls"][:2], flush=True)
print("DONE")

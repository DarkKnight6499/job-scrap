"""Offline shape tests for the 2026-10 fetchers: canned responses stand in for http(), so a parser regression fails here, not silently in production."""
import json
from unittest import mock

import job_scout as js


def fake_http(routes):
    def _http(url, data=None, headers=None, timeout=20, raw=False):
        for key, payload in routes.items():
            if key in url:
                if raw:
                    return payload if isinstance(payload, bytes) else payload.encode()
                return payload(data) if callable(payload) else payload
        raise AssertionError(f"unexpected url {url}")
    return _http


def test_taleo_pages_and_dates():
    page1 = {"requisitionList": [{"contestNo": "A1", "column": ["Analyst ", json.dumps(["US-NY-New York"]), "Oct 2, 2026"]}] * 1 + [
        {"contestNo": f"B{i}", "column": ["T", "[]", "Oct 1, 2026"]} for i in range(24)], "pagingData": {"pageSize": 25}}
    page2 = {"requisitionList": [{"contestNo": "C1", "column": ["Last", "[]", "bad date"]}], "pagingData": {"pageSize": 25}}
    pages = iter([page1, page2])
    with mock.patch.object(js, "http", fake_http({"searchjobs": lambda body: next(pages)})):
        jobs, complete = js.taleo(dict(host="h.taleo.net", section="s", portal="1"))
    assert complete and len(jobs) == 26
    assert jobs[0] == dict(id="A1", title="Analyst", location="US-NY-New York",
                           url="https://h.taleo.net/careersection/s/jobdetail.ftl?job=A1&lang=en", posted="2026-10-02")
    assert jobs[-1]["posted"] == ""


def test_hrmdirect_parses_list_and_detail():
    lst = '<a href="job-opening.php?req=11&req_loc=1&&amp;nohd#job">Compliance Analyst</a><a href="job-opening.php?req=11&&amp;nohd#job">Compliance Analyst</a>'
    detail = "<html><body>Compliance Analyst Location: New York, NY Type of Hire: Experienced Hires Division: X</body></html>"
    with mock.patch.object(js, "http", fake_http({"job-openings.php": lst, "job-opening.php": detail})):
        jobs, complete = js.hrmdirect(dict(host="opco.hrmdirect.com"))
    assert complete and len(jobs) == 1
    assert jobs[0]["id"] == "11" and jobs[0]["location"] == "New York, NY" and jobs[0]["posted"] == ""


def test_commerzbank_paging():
    item = lambda i: {"MatchedObjectId": str(i), "MatchedObjectDescriptor": {
        "ID": str(i), "PositionTitle": f"Job {i}", "PositionURI": f"u{i}", "PublicationStartDate": "2026-10-02",
        "PositionLocation": [{"CityName": "Frankfurt", "CountryName": "Germany"}]}}
    batches = iter([{"SearchResult": {"SearchResultItems": [item(i) for i in range(100)]}},
                    {"SearchResult": {"SearchResultItems": [item(100)]}}])
    with mock.patch.object(js, "http", fake_http({"api-jobs.commerzbank.com": lambda body: next(batches)})):
        jobs, complete = js.commerzbank(dict())
    assert complete and len(jobs) == 101
    assert jobs[0]["location"] == "Frankfurt, Germany" and jobs[0]["posted"] == "2026-10-02"


def test_dzbank_dates_and_desc():
    rows = [dict(jobReqId="1", stellenbezeichnung="T", standort="Singapore, SG", joblink="https://x/1", publiziert="01.10.2026", beschreibung="<p>Hi</p>"),
            dict(jobReqId="", stellenbezeichnung="skipped", joblink="")]
    with mock.patch.object(js, "http", fake_http({"sapjobs.json": rows})):
        jobs, complete = js.dzbank(dict())
    assert complete and len(jobs) == 1
    assert jobs[0]["posted"] == "2026-10-01" and jobs[0]["desc"] == "Hi"


def test_marketaxess_joins_dates_to_posts():
    jobs_json = [{"id": 5, "opened_at": "2026-09-22T09:23:00-04:00"}]
    posts = [{"job_id": 5, "title": "Research Analyst, ML", "location": {"name": "New York"}, "content": "<p>Role</p>"}]
    with mock.patch.object(js, "http", fake_http({"path=/jobs/": jobs_json, "path=/job_posts/": posts})):
        jobs, complete = js.marketaxess(dict())
    assert complete and jobs[0]["posted"] == "2026-09-22" and jobs[0]["url"].endswith("/research-analyst-ml-5")


def test_avature_details_variant():
    card = ('<div class="article article--result clearfix "><a class="link" href="https://r.example/en_US/careers/JobDetail?jobId=77"> Treasury Analyst </a>'
            '<img alt="Office Location:" /> <p> Sydney Office </p><img alt="Posted Date:" /> <p> 02 Oct 2026 </p></article>')
    with mock.patch.object(js, "http", fake_http({"SearchJobs": "<html>" + card + "</html>"})):
        jobs, complete = js.avature(dict(host="r.example", list_path="en_US/careers/SearchJobs", variant="details", page_size=9))
    assert complete
    assert jobs == [dict(id="77", title="Treasury Analyst", url="https://r.example/en_US/careers/JobDetail?jobId=77",
                         location="Sydney Office", posted="2026-10-02")]


def test_successfactors_pages_past_a_page_that_only_repeats_earlier_searches():
    def item(i):
        return f"<item><title>Job {i} (NY)</title><link>https://h/job/x/{i}/</link><pubDate>Fri, 02 Oct 2026 10:00:00 GMT</pubDate><description>d</description></item>"
    def feed(ids):
        return ("<rss><channel>" + "".join(item(i) for i in ids) + "</channel></rss>").encode()
    pages = {("(a)", 0): feed(range(20)), ("(a)", 20): feed([]), ("(b)", 0): feed(range(20)), ("(b)", 20): feed(range(20, 25))}

    def _http(url, data=None, headers=None, timeout=20, raw=False):
        q = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))
        return pages[(js.urllib.parse.unquote(q["keywords"]), int(q["startrow"]))]
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.successfactors(dict(host="h", locale="en_US", search=["a", "b"], max_per_term=100))
    assert complete and len(jobs) == 25

"""Offline tests for the real-posting-date paths added to describe() and the Taleo list: saved snippets stand in for the network."""
import json
from datetime import date, timedelta
from unittest import mock

import job_scout as js
from test_fetchers import fake_http


def _ld(stamp):
    return ('<html><script type="application/ld+json">' + json.dumps({"description": "<p>Role text</p>", "datePosted": stamp})
            + "</script></html>")


def test_icims_real_stamp_is_exact_and_synthetic_stamp_uses_sitemap():
    job = dict(id="13580", url="https://careers-x.icims.com/jobs/13580/analyst/job")
    with mock.patch.object(js, "http", fake_http({"/jobs/13580/": _ld("2026-10-09T04:00:00.000Z")})):
        assert js.describe(dict(ats="icims"), job) == ("Role text", "2026-10-09", "exact")
    js._ICIMS_SITEMAPS.clear()
    sitemap = ('<urlset><url><loc>https://careers-x.icims.com/jobs/13580/analyst/job</loc><lastmod>2026-10-10T02:55:30-04:00</lastmod></url>'
               '<url><loc>https://careers-x.icims.com/jobs/1/other/job</loc><lastmod>2026-06-05T01:00:00-04:00</lastmod></url></urlset>')
    # East West Bank stamps datePosted with the render time minus two years; that must not become an "exact" date
    with mock.patch.object(js, "http", fake_http({"sitemap.xml": sitemap, "/jobs/13580/": _ld("2024-10-11T18:33:36.336Z")})):
        assert js.describe(dict(ats="icims"), job) == ("Role text", "2026-10-10", "approx")
    js._ICIMS_SITEMAPS.clear()
    with mock.patch.object(js, "http", fake_http({"sitemap.xml": "<urlset/>", "/jobs/13580/": _ld("2024-10-11T18:33:36.336Z")})):
        assert js.describe(dict(ats="icims"), job) == ("Role text", "", "")
    js._ICIMS_SITEMAPS.clear()


def test_adp_rm_posting_date_from_search_meta():
    meta = {"jobRequisitions": [{"requisitionDescription": "<p>Credit role</p>", "postingInstructions": [
        {"timestampLastPosted": "2026-10-09T14:00:50Z", "postDate": ""}, {"timestampLastPosted": "2026-09-01T10:00:00Z"}]}]}
    routes = {"career-site": {"myJobsToken": "T"}, "search-meta/5001": meta}
    with mock.patch.object(js, "http", fake_http(routes)):
        # list jobs already carry desc: it is kept, only the date is added
        assert js.describe(dict(ats="adp_rm", domain="d"), dict(id="5001", desc="From list")) == ("From list", "2026-10-09", "exact")
        # a queue entry has no desc: the endpoint's description is used
        assert js.describe(dict(ats="adp_rm", domain="d"), dict(id="5001")) == ("Credit role", "2026-10-09", "exact")
    routes["search-meta/5001"] = {"jobRequisitions": [{"requisitionDescription": "x", "postingInstructions": [{"timestampLastPosted": ""}]}]}
    with mock.patch.object(js, "http", fake_http(routes)):
        assert js.describe(dict(ats="adp_rm", domain="d"), dict(id="5001")) == ("x", "", "")


def test_avature_feed_date_found_by_quoted_title():
    feed = ('<rss><channel><item><title>Other</title><link>https://h/careers/JobDetail/NY-Other/14000</link><pubDate>Wed, 27 Nov 2024 00:00:00 +0000</pubDate></item>'
            '<item><title>AI Fluency</title><link>https://h/careers/JobDetail/NY-AI-Fluency-Program-Manager/14291</link>'
            '<pubDate>Tue, 22 Sep 2026 00:00:00 +0000</pubDate></item></channel></rss>')
    seen = []

    def http(url, **kw):
        seen.append(url)
        return feed.encode() if "/feed/" in url else b"<html><body>Program role</body></html>"
    c = dict(ats="avature", host="h", list_path="careers/OpenRoles", feed=True)
    job = dict(id="14291", title="AI Fluency: Program Manager", url="https://h/careers/JobDetail/NY-AI/14291", posted="")
    with mock.patch.object(js, "http", http):
        text, posted, source = js.describe(c, job)
        assert (posted, source) == ("2026-09-22", "exact") and "Program role" in text
        assert any("search=%22AI%20Fluency%20%20Program%20Manager%22" in u for u in seen)  # punctuation dropped, title quoted
        assert js.describe(c, dict(job, id="99"))[1:] == ("", "")  # a job the feed does not list stays undated
        assert js.describe(dict(c, feed=False), job)[1:] == ("", "")  # tenants without a feed are untouched


def test_talentsoft_update_date_is_approx():
    page = ('<div class="ts-offer-page__maj-date"><h3> Date de mise à jour </h3> 09/10/2026 </div>'
            '<div class="ts-offer-page__body">Analyste role</div>')
    with mock.patch.object(js, "http", fake_http({"/job/": page})):
        text, posted, source = js.describe(dict(ats="talentsoft"), dict(id="1", url="https://j/job/x_1.aspx"))
    assert (posted, source) == ("2026-10-09", "approx") and "Analyste role" in text
    with mock.patch.object(js, "http", fake_http({"/job/": '<div class="ts-offer-page__body">No date</div>'})):
        assert js.describe(dict(ats="talentsoft"), dict(id="1", url="https://j/job/x_1.aspx"))[1:] == ("", "")


def test_taleo_without_date_column_buckets_ages():
    row = lambda n: {"contestNo": n, "column": [n, "Analyst " + n, json.dumps(["OH-Fairfield"])]}
    everything = [row("1"), row("2"), row("3"), row("4")]
    by_bucket = {"1": [], "2": [row("1")], "3": [row("1"), row("2")], "4": [row("1"), row("2"), row("3")],
                 "5": [row("1"), row("2"), row("3")], "6": [row("1"), row("2"), row("3")]}  # job 4 is older than 28 days

    def search(body):
        chosen = body["filterSelectionParam"]["searchFilterSelections"][0]["selectedValues"]
        return {"requisitionList": by_bucket[chosen[0]] if chosen else everything, "pagingData": {"pageSize": 25}}
    with mock.patch.object(js, "http", fake_http({"searchjobs": search})):
        jobs, complete = js.taleo(dict(host="h.taleo.net", section="ex", portal="1", columns=[1, 2, -1]))
    today = date.today()
    assert complete and [(j["id"], j["posted"], j.get("posted_source")) for j in jobs] == [
        ("1", (today - timedelta(days=1)).isoformat(), "approx"),  # Yesterday bucket
        ("2", (today - timedelta(days=4)).isoformat(), "approx"),  # Last 7 Days: midpoint of 2..7
        ("3", (today - timedelta(days=11)).isoformat(), "approx"),  # Last 14 Days: midpoint of 8..14
        ("4", "", None)]


def test_taleo_age_bucket_failure_keeps_list_and_dates_found():
    def search(body):
        chosen = body["filterSelectionParam"]["searchFilterSelections"][0]["selectedValues"]
        if chosen and chosen[0] != "1":
            raise RuntimeError("500")
        return {"requisitionList": [{"contestNo": "1", "column": ["1", "T", "[]"]}], "pagingData": {"pageSize": 25}}
    with mock.patch.object(js, "http", fake_http({"searchjobs": search})):
        jobs, complete = js.taleo(dict(host="h", section="s", portal="1", columns=[1, 2, -1]))
    assert complete and jobs[0]["posted"] == date.today().isoformat()  # bucket 1 (Today) answered before the failure


def test_retry_enrichment_dates_undated_detail_ats_entries(monkeypatch):
    monkeypatch.setattr(js, "describe", lambda c, j: ("text", "2026-10-09", "approx"))
    e = dict(id="Valley:5001", company="Valley", link="https://x", role="Analyst", location="NY", posted="", state="new",
             description_status="ok", site=None)
    js._retry_enrichment([e], dict(companies=[dict(name="Valley", ats="adp_rm")]), [])
    assert (e["posted"], e["posted_source"]) == ("2026-10-09", "approx")

from datetime import date
from unittest import mock

import job_scout as js

LD = '<script type="application/ld+json">{"@type":"JobPosting","description":"<p>Credit work</p>","datePosted":"%s"}</script>'


def _desc(stamp):
    raw = (LD % stamp).encode()
    with mock.patch.object(js, "http", return_value=raw):
        return js.describe({"ats": "paylocity", "name": "X"}, {"id": "1", "url": "http://x/1"})


def test_page_date_replaces_list_date():
    text, posted, src = _desc("2026-09-17T20:53:43-05:00")
    assert "Credit work" in text and posted == "2026-09-17" and src == "exact"


def test_todays_date_is_a_render_stamp_and_ignored():
    _t, posted, src = _desc(date.today().isoformat() + "T08:00:00-05:00")
    assert posted == "" and src == ""


def test_fetcher_prefers_page_date_over_list_date():
    board = ('window.pageData = {"Jobs":[{"JobId":7,"JobTitle":"A","JobLocation":{"City":"X","State":"NJ"},'
             '"PublishedDate":"2026-10-22T00:00:00"}]};\n').encode()
    page = (LD % "2026-09-17T20:53:43-05:00").encode()
    with mock.patch.object(js, "http", side_effect=lambda url, **kw: board if "/jobs/All/" in url else page):
        jobs, ok = js.paylocity({"board": "http://x/recruiting/jobs/All/g/n"})
    assert ok and jobs[0]["posted"] == "2026-09-17"

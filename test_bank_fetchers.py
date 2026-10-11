"""Offline shape tests for taleo_be, talentsoft and bpce: saved snippets of each site's markup/JSON stand in for the network."""
import json
from unittest import mock

import job_scout as js
from test_fetchers import fake_http


class FakeResp:
    def __init__(self, payload):
        self.payload = payload.encode() if isinstance(payload, str) else payload

    def read(self):
        return self.payload


class FakeOpener:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def open(self, req, timeout=None):
        self.calls.append(req)
        if "/jobs/search/" in req.full_url:
            return FakeResp('<input type = "hidden" name="tsstoken" id = "tsstoken" value ="TOK="/>')
        return FakeResp(json.dumps({"Result": self.pages[int(req.full_url.split("page_index=")[1].split("&")[0]) - 1]}))


def test_taleo_be_pages_token_and_rows():
    row = lambda i: (f'<div id="job_list_{i}" class="job_list_row"><p><a href="https://h/jobs/t-{i}" class="job_link font_bold">Title &amp; {i}</a></p>'
                     f'<span class="location"> Salt Lake City, Utah, United States </span></div>')
    pages = [f'<div id="jPaginateNumPages">2.0</div>{row(1)}{row(2)}', f'<div id="jPaginateNumPages">2.0</div>{row(3)}']
    op = FakeOpener(pages)
    with mock.patch.object(js.urllib.request, "build_opener", return_value=op):
        jobs, complete = js.taleo_be(dict(host="h", search_id="9", site_name="S"))
    assert complete and [j["id"] for j in jobs] == ["1", "2", "3"]
    assert jobs[0]["title"] == "Title & 1" and jobs[0]["location"] == "Salt Lake City, Utah, United States"
    assert op.calls[-1].get_header("Tss-token") == "TOK="  # the AJAX layer rejects calls without it


def test_talentsoft_cards_and_stop_on_repeat():
    card = lambda i, city: (f'<div class="ts-offer-card Layer" onclick="x"><h3><a class="ts-offer-card__title-link  " href="/job/job-x-{i}_{i}.aspx" title="r">'
                            f' Analyst {i} </a></h3><ul><li>Permanent</li><li>United States Of America</li><li class="noBorder">{city}</li></ul></div>')
    seq = iter([(card(1, "NEW YORK") + card(2, "CHICAGO")).encode(), card(1, "NEW YORK").encode()])
    with mock.patch.object(js, "http", lambda url, **kw: next(seq)):
        jobs, complete = js.talentsoft(dict(host="j.example.com"))
    assert complete and [j["id"] for j in jobs] == ["1", "2"]
    assert jobs[0]["location"] == "NEW YORK, United States Of America" and jobs[0]["title"] == "Analyst 1"
    assert jobs[0]["url"] == "https://j.example.com/job/job-x-1_1.aspx"


def test_bpce_locations_dates_and_completeness():
    item = lambda n, city, region, country: {"job_number": n, "title": f"Job {n}", "date": "2026-10-09", "description": "<p>Desc</p>",
                                             "link": {"url": f"/en/job/j{n}"},
                                             "localisations": [{"city": city, "region": region, "country": country}]}
    data = {"data": {"total": 2, "items": [item("1", "New York", "United States", "International"), item("2", "Paris", "Ile-de-France", "France")]}}
    with mock.patch.object(js, "http", fake_http({"search/jobs": data})):
        jobs, complete = js.bpce(dict(host="r.example.com"))
    assert complete and [j["location"] for j in jobs] == ["New York, United States", "Paris, France"]
    assert jobs[0]["posted"] == "2026-10-09" and jobs[0]["desc"] == "Desc" and jobs[0]["url"] == "https://r.example.com/en/job/j1"
    data["data"]["total"] = 5
    with mock.patch.object(js, "http", fake_http({"search/jobs": data})):
        assert js.bpce(dict(host="r.example.com"))[1] is False  # fewer items than the site's total


def test_paylocity_page_data_and_dates():
    page = ('<script>window.pageData = {"Jobs": [{"JobId": 7, "JobTitle": "Teller", "LocationName": "Locust Valley", "PublishedDate": "2026-10-07T14:56:00-05:00",'
            ' "JobLocation": {"City": "Locust Valley", "State": "NY"}}]};\n</script>')
    with mock.patch.object(js, "http", fake_http({"paylocity.com/recruiting/jobs": page})):
        jobs, complete = js.paylocity(dict(board="https://recruiting.paylocity.com/recruiting/jobs/All/g/x"))
    assert complete and jobs == [dict(id="7", title="Teller", location="Locust Valley, NY",
                                      url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/7", posted="2026-10-07")]


def test_page_date_formats_and_describe_refines_posted():
    assert js._page_date('{"datePosted": "Oct 1, 2026"}') == "2026-10-01"
    assert js._page_date('{"datePosted":"2026-08-18T10:00:00Z"}') == "2026-08-18"
    assert js._page_date('{"datePosted": "soon"}') == "" and js._page_date("<html></html>") == ""
    page = '<script>{"datePosted": "Oct 1, 2026"}</script><body>Analyst role</body>'
    with mock.patch.object(js, "http", fake_http({"jobdetail": page})):
        text, posted, source = js.describe(dict(ats="kpmg"), dict(url="https://k/jobdetail/?jobId=1", id="1"))
    assert (posted, source) == ("2026-10-01", "exact") and "Analyst role" in text


def test_taleo_custom_column_order():
    page = {"requisitionList": [{"contestNo": "9", "column": ["9", "Risk Analyst ", json.dumps(["OH-Fairfield"])]}], "pagingData": {"pageSize": 25}}
    with mock.patch.object(js, "http", fake_http({"searchjobs": page})):
        jobs, complete = js.taleo(dict(host="h.taleo.net", section="ex", portal="1", columns=[1, 2, -1]))
    assert complete and jobs[0]["title"] == "Risk Analyst" and jobs[0]["location"] == "OH-Fairfield" and jobs[0]["posted"] == ""

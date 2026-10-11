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

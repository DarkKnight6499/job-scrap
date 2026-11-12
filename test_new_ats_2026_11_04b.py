"""Offline tests for the second 2026-11-04 fetcher batch; all HTTP is mocked."""
from unittest import mock

import pytest

import job_scout as js
import validate_config as vc
from test_fetchers import fake_http
from test_new_ats_2026_11_04 import posting_page

TEAMTAILOR = dict(host="careers.canoeintelligence.com")
COMEET = dict(host="https://www.comeet.com/jobs/lili/A6.009")
KULA = dict(slug="onshore")


@pytest.fixture(autouse=True)
def no_live_http():
    with mock.patch.object(js, "http", side_effect=AssertionError("HTTP must be mocked")):
        yield


def rss_item(jid, **kw):
    fields = dict(title="Quant &amp; Risk", link=f"https://{TEAMTAILOR['host']}/jobs/{jid}-quant",
                  pubDate="Fri, 30 Oct 2026 12:00:00 GMT", description="&lt;p&gt;Manage risk&lt;/p&gt;")
    fields.update(kw)
    return '<item>' + ''.join(f'<{key}>{value}</{key}>' for key, value in fields.items()) + '</item>'


def rss_page(items):
    return '<rss version="2.0" xmlns:job="urn:jobs"><channel>' + ''.join(items) + '</channel></rss>'


def test_teamtailor_rss_dates_locations_description_and_duplicates():
    item = rss_item("123", **{"job:location": "New York", "location": "London", "remote_status": "hybrid"})
    raw = rss_page([item, item, rss_item("456", pubDate="invalid", description="")])
    with mock.patch.object(js, "http", side_effect=fake_http({"/jobs.rss": raw})) as request:
        jobs, complete = js.teamtailor(TEAMTAILOR)
    assert complete and [j["id"] for j in jobs] == ["123", "456"]
    assert jobs[0]["title"] == "Quant & Risk" and jobs[0]["desc"] == "Manage risk"
    assert jobs[0]["location"] == "New York; London; Hybrid"
    assert jobs[0]["posted"] == "2026-10-30" and jobs[1]["posted"] == ""
    assert jobs[0]["url"] == f"https://{TEAMTAILOR['host']}/jobs/123-quant"
    assert request.call_count == 1


def test_teamtailor_rss_pagination_and_empty_board():
    calls = []
    pages = iter([rss_page([rss_item(str(i)) for i in range(100)]), rss_page([rss_item("100")])])

    def _http(url, **kw):
        calls.append(url)
        return next(pages).encode()
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.teamtailor(TEAMTAILOR)
    assert complete and len(jobs) == 101
    assert calls == [f"https://{TEAMTAILOR['host']}/jobs.rss", f"https://{TEAMTAILOR['host']}/jobs.rss?offset=100"]
    with mock.patch.object(js, "http", fake_http({"/jobs.rss": rss_page([])})):
        assert js.teamtailor(TEAMTAILOR) == ([], True)


@pytest.mark.parametrize("limit", [1, 2])
def test_teamtailor_rss_cap_and_repeated_page_are_incomplete(limit):
    raw = rss_page([rss_item(str(i)) for i in range(100)])
    with mock.patch.object(js, "http", fake_http({"/jobs.rss": raw})):
        jobs, complete = js.teamtailor(dict(TEAMTAILOR, max_pages=limit))
    assert len(jobs) == 100 and not complete


@pytest.mark.parametrize("feed", ["<html>Unavailable</html>", "broken", "<rss/>", None])
def test_teamtailor_html_fallback(feed):
    listing = '<h2>1 jobs</h2><a href="/jobs/123-quant">Quant</a>' * 2

    def _http(url, **kw):
        if "/jobs.rss" in url:
            if feed is None:
                raise js.urllib.error.HTTPError(url, 404, "Not found", {}, None)
            return feed.encode()
        return fake_http({"/jobs/123-quant": posting_page(datePosted="2026-10-30"), "/jobs": listing})(url, **kw)
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.teamtailor(TEAMTAILOR)
    assert complete and len(jobs) == 1 and jobs[0]["id"] == "123"
    assert jobs[0]["location"] == "Stamford, CT, United States"
    assert jobs[0]["posted"] == "2026-10-30" and jobs[0]["desc"] == "Manage risk"


@pytest.mark.parametrize("code", [403, 429, 503])
def test_teamtailor_http_failures_do_not_become_empty_boards(code):
    error = js.urllib.error.HTTPError("u", code, "Unavailable", {}, None)
    with mock.patch.object(js, "http", side_effect=error):
        with pytest.raises(js.urllib.error.HTTPError):
            js.teamtailor(TEAMTAILOR)


def comeet_row(jid, **kw):
    row = dict(uid=jid, name=" Analyst ", location={"name": "New York"},
               url_active_page=f"https://example.com/jobs/{jid}", time_updated="2026-10-30T12:00:00Z",
               details=[{"name": "Description", "value": "<p>Manage risk</p>"}])
    row.update(kw)
    return row


@pytest.mark.parametrize("settings", [
    '<script>var comeetvar = {}; comeetvar.company = "A6.009"; comeetvar.token = "public+token";</script>',
    '<script>var settings = {"company_uid": "A6.009", "token": "public+token"};</script>',
    '<script>COMEET.init({"token":"public+token", "company-uid":"A6.009"});</script>',
    '<div data-company-uid="A6.009" data-token="public+token"></div>',
    '<script src="https://www.comeet.co/careers-api/2.0/company/A6.009/positions?token=public%2Btoken&amp;details=true"></script>',
])
def test_comeet_discovers_credentials_and_maps_positions(settings):
    calls = []
    rows = [comeet_row("1"), comeet_row("1"), comeet_row("2", location=None, time_updated=None,
            workplace_type="Remote", details=None, url_active_page=None, url_comeet_hosted_page="https://example.com/2")]

    def _http(url, **kw):
        calls.append((url, kw))
        return settings.encode() if kw.get("raw") else rows
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.comeet(COMEET)
    assert complete and [j["id"] for j in jobs] == ["1", "2"]
    assert calls[0][0] == COMEET["host"]
    assert calls[1][0] == "https://www.comeet.co/careers-api/2.0/company/A6.009/positions?token=public%2Btoken&details=true"
    assert calls[1][1]["timeout"] == 60
    assert jobs[0]["title"] == "Analyst" and jobs[0]["location"] == "New York"
    assert jobs[0]["url"] == "https://example.com/jobs/1" and jobs[0]["desc"] == "Manage risk"
    assert jobs[0]["posted"] == "2026-10-30" and jobs[0]["posted_source"] == "approx"
    assert jobs[1]["location"] == "Remote" and jobs[1]["posted"] == "" and jobs[1]["posted_source"] == ""
    assert jobs[1]["url"] == "https://example.com/2"


def test_comeet_explicit_credentials_skip_discovery_and_allow_empty_board():
    with mock.patch.object(js, "http", side_effect=fake_http({"/company/explicit/positions?token=a%26b": []})) as request:
        assert js.comeet(dict(COMEET, uid="explicit", token="a&b")) == ([], True)
    assert request.call_count == 1


def test_comeet_partial_override_and_location_fallback():
    rows = [comeet_row("1", location={"city": "New York", "state": "NY", "country": "US"})]
    with mock.patch.object(js, "http", fake_http({"/company/override/positions?token=found": rows,
                           COMEET["host"]: '<script>var config = {"uid":"found", "token":"found"}</script>'})):
        jobs, complete = js.comeet(dict(COMEET, uid="override"))
    assert complete and jobs[0]["location"] == "New York, NY, US"


def test_comeet_missing_credentials_and_error_response_raise():
    with mock.patch.object(js, "http", fake_http({COMEET["host"]: "<h1>Careers</h1>"})):
        with pytest.raises(ValueError, match="uid or token missing"):
            js.comeet(COMEET)
    with mock.patch.object(js, "http", fake_http({"/positions": {"error": "invalid token"}})):
        with pytest.raises(ValueError, match="must be a list"):
            js.comeet(dict(COMEET, uid="a", token="b"))


def test_kula_job_links_jsonld_and_duplicate_ids():
    listing = ('<h2>Open Positions</h2><a href="/onshore/37841-sales-operations-associate">Apply Now</a>'
               '<a href="/onshore/37841-renamed?source=site">Duplicate</a>'
               '<a href="/onshore/jobs/22">Apply Now</a>'
               '<a href="/other/33-role">Other company</a><a href="https://elsewhere.com/onshore/44-role">Other host</a>')
    routes = {"/37841-sales-operations-associate": posting_page(title="Sales Operations Associate", datePosted="2026-10-30",
               jobLocation=[{"address": "New York"}, {"address": "London"}]),
              "/jobs/22": '<h1>Quant &amp; Risk</h1><p>Do work</p>',
              "https://careers.kula.ai/onshore": listing}
    with mock.patch.object(js, "http", side_effect=fake_http(routes)) as request:
        jobs, complete = js.kula(KULA)
    assert complete and [j["id"] for j in jobs] == ["37841", "22"]
    assert jobs[0]["title"] == "Sales Operations Associate" and jobs[0]["location"] == "New York; London"
    assert jobs[0]["posted"] == "2026-10-30" and jobs[0]["desc"] == "Manage risk"
    assert jobs[1]["title"] == "Quant & Risk" and jobs[1]["posted"] == ""
    assert "Do work" in jobs[1]["desc"] and request.call_count == 3


@pytest.mark.parametrize("raw,complete", [("<h2>No open positions</h2>", True), ("Service unavailable", False),
                                         ("<h2>Open Positions</h2>", False)])
def test_kula_empty_and_unknown_pages(raw, complete):
    with mock.patch.object(js, "http", fake_http({"/onshore": raw})):
        assert js.kula(KULA) == ([], complete)


@pytest.mark.parametrize("prefix", ['<h2>2 jobs</h2>', '<a href="?page=2" rel="next">Next</a>'])
def test_kula_partial_html_is_incomplete(prefix):
    with mock.patch.object(js, "http", fake_http({"/onshore/1-role": posting_page(),
                           "https://careers.kula.ai/onshore": prefix + '<a href="/onshore/1-role">Analyst</a>'})):
        jobs, complete = js.kula(KULA)
    assert len(jobs) == 1 and not complete


@pytest.mark.parametrize("ats,company", [("teamtailor", TEAMTAILOR), ("comeet", COMEET), ("kula", KULA)])
def test_new_ats_dispatch_validation_and_describe(ats, company):
    config = dict(company, ats=ats, name="Example")
    assert js.FETCH[ats] is getattr(js, ats)
    assert vc.validate({"companies": [config]}, []) == ([], [])
    missing = dict(config)
    missing.pop(vc.REQUIRED_FIELDS[ats][0])
    assert vc.validate({"companies": [missing]}, [])[0]
    with mock.patch.object(js, "http", fake_http({"https://example/job": posting_page(datePosted="2026-10-30")})):
        text, posted, source = js.describe(config, dict(id="a", url="https://example/job"))
    assert text == "Manage risk"
    assert (posted, source) == (("", "") if ats == "comeet" else ("2026-10-30", "exact"))


@pytest.mark.parametrize("field", ["uid", "token"])
@pytest.mark.parametrize("value", [None, "", " ", [], 42])
def test_comeet_optional_credentials_reject_invalid_values(field, value):
    config = dict(COMEET, ats="comeet", name="Example")
    config[field] = value
    errors, _ = vc.validate({"companies": [config]}, [])
    assert any(f"{field} must be a nonempty string" in error for error in errors)


@pytest.mark.parametrize("ats,company", [("teamtailor", TEAMTAILOR), ("comeet", COMEET), ("kula", KULA)])
def test_new_ats_shared_boards_are_rejected(ats, company):
    first = dict(company, ats=ats, name="A")
    second = dict(first, name="B")
    errors, groups = vc.validate({"companies": [first, second]}, [])
    assert errors and groups[0]["kind"] == "shared endpoint"


def test_comeet_embedded_position_list_without_token():
    page = '<script>x = {"comeet_data":{"departments":{"Sales":[{"uid":"54.27C","name":"SDR - NYC","city":"New York","location_name":"US","url_active_page":"https://example.com/co/54.27C"}]}}}</script>'
    with mock.patch.object(js, "http", return_value=page.encode()):
        jobs, complete = js.comeet({"name": "Agora", "ats": "comeet", "host": "https://example.com/careers/"})
    assert complete
    assert [(j["id"], j["title"], j["location"]) for j in jobs] == [("54.27C", "SDR - NYC", "New York, US")]

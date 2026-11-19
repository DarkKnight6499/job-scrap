"""Offline tests for the Y Combinator company-jobs fetcher; all HTTP is mocked."""
import html
import json
from unittest import mock

import pytest

import job_scout as js
import validate_config as vc


@pytest.fixture(autouse=True)
def no_live_http():
    with mock.patch.object(js, "http", side_effect=AssertionError("HTTP must be mocked")):
        yield


def _page(postings):
    data = json.dumps({"component": "WaasShowJobsPage", "props": {"jobPostings": postings}})
    return f'<div id="app" data-page="{html.escape(data)}"></div>'.encode()


def _post(jid, title, visa, **extra):
    return dict(id=jid, title=title, url=f"/companies/x/jobs/{jid}-t", location="New York, NY, US / Remote",
                visa=visa, minExperience="1+ years", salaryRange="$100K - $120K", createdAt="2026-11-10T09:00:00Z", **extra)


def test_yc_parses_postings_visa_and_dedupes():
    raw = _page([_post(1, " Treasury Analyst ", "Will sponsor"), _post(1, "Treasury Analyst", "Will sponsor"),
                 _post(2, "Risk Analyst", "US citizen/visa only")])
    with mock.patch.object(js, "http", return_value=raw):
        jobs, complete = js.yc(dict(slug="x"))
    assert complete and [j["id"] for j in jobs] == ["1", "2"]
    assert jobs[0]["title"] == "Treasury Analyst" and jobs[0]["posted"] == "2026-11-10"
    assert jobs[0]["url"] == "https://www.ycombinator.com/companies/x/jobs/1-t"
    assert jobs[0]["location"] == "New York, NY, US; Remote" and "Visa field: Will sponsor." in jobs[0]["desc"]


def test_yc_citizen_only_visa_reads_as_no_sponsorship():
    raw = _page([_post(2, "Risk Analyst", "US citizen/visa only")])
    with mock.patch.object(js, "http", return_value=raw):
        jobs, _ = js.yc(dict(slug="x"))
    assert js.classify_sponsorship(jobs[0]["desc"])[0] == "OPT Only"


def test_yc_missing_or_broken_page_is_empty():
    for body in (b"<html>no data</html>", b'<div data-page="{not json}"></div>'):
        with mock.patch.object(js, "http", return_value=body):
            assert js.yc(dict(slug="x")) == ([], True)


def test_yc_is_registered_and_requires_slug():
    assert js.FETCH["yc"] is js.yc
    assert vc.REQUIRED_FIELDS["yc"] == ("slug",)

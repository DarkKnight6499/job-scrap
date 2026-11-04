"""Offline tests for the 2026-11-04 fetchers and filters; all HTTP is mocked."""
import json
from unittest import mock

import pytest

import job_scout as js
import validate_config as vc
from test_fetchers import fake_http

LEVER = dict(slug="bfsaul", team="Chevy Chase Trust")
JAZZ = dict(host="derivativepath.applytojob.com")
WORKABLE = dict(slug="bestex-research")
NAS = dict(host="careers.osaic.com")
TALENTBREW = dict(host="careers.adeccogroup.com", filter="lhh")


def posting_page(**kw):
    row = {"@type": "JobPosting", "title": " Analyst ", "description": "<p>Manage <b>risk</b></p>",
           "jobLocation": {"address": {"addressLocality": "Stamford", "addressRegion": "CT",
                                        "addressCountry": {"name": "United States"}}}}
    row.update(kw)
    return '<script type="application/ld+json">' + json.dumps({"@graph": [row]}) + '</script>'


def test_lever_team_exact_match_and_created_at():
    rows = [dict(id=str(i), text="Analyst", hostedUrl=f"https://jobs.lever.co/bfsaul/{i}",
                 categories={"team": team, "location": "Bethesda"}, createdAt=1791079200000)
            for i, team in enumerate(["Chevy Chase Trust", "Other", "chevy chase trust", None])]
    rows.append(dict(id="missing", text="Other", hostedUrl="u", categories=None))
    with mock.patch.object(js, "http", fake_http({"/v0/postings/bfsaul?mode=json": rows})):
        jobs, complete = js.lever(LEVER)
        unfiltered, _ = js.lever(dict(slug="bfsaul"))
    assert complete and [j["id"] for j in jobs] == ["0"]
    assert jobs[0]["posted"] == "2026-10-04" and jobs[0]["location"] == "Bethesda"
    assert len(unfiltered) == 5 and unfiltered[-1]["posted"] == ""


def test_jazzhr_links_dates_remote_and_duplicates():
    listing = '<h2>Current Openings</h2><a href="/apply/Ab1/Analyst">Analyst</a>' * 2
    listing += "<a href='https://derivativepath.applytojob.com/apply/Cd2/Quant'>Quant &amp; Risk</a>"
    routes = {"/apply/Ab1/": posting_page(datePosted="2026-10-30T12:00:00Z"),
              "/apply/Cd2/": '<li><span class="fa fa-map-marker"></span>Remote</li><p>Do work</p>',
              "https://derivativepath.applytojob.com": listing}
    with mock.patch.object(js, "http", fake_http(routes)):
        jobs, complete = js.jazzhr(JAZZ)
    assert complete and [j["id"] for j in jobs] == ["Ab1", "Cd2"]
    assert jobs[0]["title"] == "Analyst" and jobs[0]["posted"] == "2026-10-30"
    assert jobs[0]["location"] == "Stamford, CT, United States" and jobs[0]["desc"] == "Manage risk"
    assert jobs[1]["title"] == "Quant & Risk" and jobs[1]["location"] == "Remote"
    assert jobs[1]["posted"] == "" and "Do work" in jobs[1]["desc"]
    assert jobs[0]["url"] == "https://derivativepath.applytojob.com/apply/Ab1/Analyst"


def test_jazzhr_empty_and_unrecognized_boards():
    for raw, expected in [("<h2>Current Openings</h2>", True), ("Service unavailable", False)]:
        with mock.patch.object(js, "http", fake_http({JAZZ["host"]: raw})):
            assert js.jazzhr(JAZZ) == ([], expected)


def workable_row(jid, **kw):
    row = dict(shortcode=jid, title=" Quant ", location={"city": "Stamford", "country": "United States"},
               published="2026-10-29T12:00:00Z", remote=False)
    row.update(kw)
    return row


def test_workable_post_pagination_overlap_dates_and_remote():
    calls = []
    pages = iter([dict(results=[workable_row("A")], nextPage="next", total=2),
                  dict(results=[workable_row("A"), workable_row("B", remote=True, published=None, location=None)], total=2)])

    def _http(url, data=None, **kw):
        calls.append((url, data))
        return next(pages)
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.workable(WORKABLE)
    assert complete and [j["id"] for j in jobs] == ["A", "B"]
    assert calls[0][0] == "https://apply.workable.com/api/v3/accounts/bestex-research/jobs"
    assert calls[0][1]["query"] == "" and "token" not in calls[0][1]
    assert calls[1][1]["token"] == "next"
    assert jobs[0]["title"] == "Quant" and jobs[0]["posted"] == "2026-10-29"
    assert jobs[0]["location"] == "Stamford, United States"
    assert jobs[1]["location"] == "Remote" and jobs[1]["posted"] == ""
    assert jobs[0]["url"] == "https://apply.workable.com/bestex-research/j/A/"


@pytest.mark.parametrize("pages,limit", [
    ([dict(results=[workable_row("A")], total=2)], 40),
    ([dict(results=[workable_row("A")], nextPage="x", total=2)], 1),
    ([dict(results=[workable_row("A")], nextPage="x"), dict(results=[], nextPage="y")], 40),
    ([dict(results=[workable_row("A")], nextPage="x"), dict(results=[workable_row("A")], nextPage="x")], 40),
])
def test_workable_incomplete_pages(pages, limit):
    responses = iter(pages)
    with mock.patch.object(js, "http", lambda *a, **kw: next(responses)):
        jobs, complete = js.workable(dict(WORKABLE, max_pages=limit))
    assert len(jobs) == 1 and not complete


def test_workable_empty_board():
    with mock.patch.object(js, "http", fake_http({"/jobs": dict(results=[], total=0)})):
        assert js.workable(WORKABLE) == ([], True)


def test_nas_sitemap_namespace_lastmod_multilocation_and_duplicates():
    sitemap = '''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://careers.osaic.com/search/jobdetails/analyst/a</loc><lastmod>2026-11-02T10:00:00Z</lastmod></url>
      <url><loc>https://careers.osaic.com/search/jobdetails/analyst/a</loc></url>
      <url><loc>https://careers.osaic.com/search/jobdetails/quant/b</loc></url>
      <url><loc>https://careers.osaic.com/Creative/Benefits</loc></url></urlset>'''
    locations = [{"address": {"addressLocality": "Atlanta", "addressRegion": "GA"}},
                 {"address": "Remote USA"}]
    with mock.patch.object(js, "http", fake_http({"jobs-site.xml": sitemap,
                           "/analyst/a": posting_page(jobLocation=locations),
                           "/quant/b": "<h1>Quant &amp; Risk</h1><li><strong>Location:</strong>Atlanta, GA</li><p>Do work</p>"})):
        jobs, complete = js.nas_recruitment(NAS)
    assert complete and [j["id"] for j in jobs] == ["a", "b"]
    assert jobs[0]["title"] == "Analyst" and jobs[0]["location"] == "Atlanta, GA; Remote USA"
    assert jobs[0]["posted"] == "2026-11-02" and jobs[0]["posted_source"] == "approx"
    assert jobs[0]["desc"] == "Manage risk" and jobs[1]["posted"] == ""
    assert jobs[1]["title"] == "Quant & Risk"
    assert jobs[1]["location"] == "Atlanta, GA"


@pytest.mark.parametrize("raw", ["<html>Unavailable</html>", "<sitemapindex/>", "broken"])
def test_nas_invalid_sitemap_raises(raw):
    with mock.patch.object(js, "http", fake_http({"jobs-site.xml": raw})):
        with pytest.raises((ValueError, js.ET.ParseError)):
            js.nas_recruitment(NAS)


def talentbrew_card(jid, title, path="role", dated=False):
    return (f'<a href="/en/job/city/{path}/1/{jid}" data-job-id="{jid}"><h2>{title}</h2>'
            '<span class="job-location">New York</span>'
            + ('<span class="job-date-posted">10/30/2026</span>' if dated else '') + '</a>')


def test_talentbrew_filter_url_title_undated_and_all_pages():
    bodies = iter([{'results': '<div data-total-pages="2">' + talentbrew_card("1", "Adecco Analyst", dated=True)},
                   {'results': talentbrew_card("2", "Analyst", path="lhh-analyst") + talentbrew_card("3", "LHH Consultant")}])
    with mock.patch.object(js, "http", lambda *a, **kw: next(bodies)):
        jobs, complete = js.talentbrew(TALENTBREW)
    assert complete and [j["id"] for j in jobs] == ["2", "3"]
    assert all(j["posted"] == "" for j in jobs)
    with mock.patch.object(js, "http", fake_http({"/search-jobs/results": {"results": talentbrew_card("1", "Adecco Analyst", dated=True)}})):
        jobs, complete = js.talentbrew(dict(host=TALENTBREW["host"]))
    assert complete and jobs[0]["posted"] == "2026-10-30"


def test_talentbrew_filtered_cap_is_incomplete():
    with mock.patch.object(js, "http", fake_http({"/search-jobs/results": {"results": '<div data-total-pages="2">'}})):
        assert js.talentbrew(dict(TALENTBREW, max_pages=1)) == ([], False)


@pytest.mark.parametrize("ats,company", [("jazzhr", JAZZ), ("workable", WORKABLE), ("nas_recruitment", NAS)])
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
    assert (posted, source) == (("", "") if ats == "nas_recruitment" else ("2026-10-30", "exact"))


@pytest.mark.parametrize("ats,company,field", [("lever", LEVER, "team"), ("talentbrew", TALENTBREW, "filter")])
@pytest.mark.parametrize("value", [None, "", " ", [], 42])
def test_optional_filters_reject_invalid_values(ats, company, field, value):
    config = dict(company, ats=ats, name="Example")
    config[field] = value
    errors, _ = vc.validate({"companies": [config]}, [])
    assert any(f"{field} must be a nonempty string" in error for error in errors)


def test_filters_do_not_hide_shared_endpoints():
    for ats, company, field in [("lever", LEVER, "team"), ("talentbrew", TALENTBREW, "filter")]:
        first = dict(company, ats=ats, name="A")
        second = dict(first, name="B")
        second[field] = "Other"
        errors, groups = vc.validate({"companies": [first, second]}, [])
        assert errors and groups[0]["kind"] == "shared endpoint"

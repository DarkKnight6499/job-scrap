"""Offline tests for the 2026-11-10 fetcher batch; all HTTP is mocked."""
from unittest import mock

import pytest

import job_scout as js
import validate_config as vc
from test_fetchers import fake_http


@pytest.fixture(autouse=True)
def no_live_http():
    with mock.patch.object(js, "http", side_effect=AssertionError("HTTP must be mocked")):
        yield


def test_breezy_list_locations_remote_dates_and_duplicates():
    row = dict(id="a1", name=" Treasury Analyst ", url="https://x.breezy.hr/p/a1-treasury",
               published_date="2026-10-20T04:22:07.464Z",
               location=dict(name="New York, NY", is_remote=True),
               locations=[dict(name="New York, NY"), dict(name="London")])
    rows = [row, row, dict(id="b2", name="Quant", url="https://x.breezy.hr/p/b2", location=dict(name="Hong Kong"))]
    with mock.patch.object(js, "http", fake_http({"/json": rows})) as request:
        jobs, complete = js.breezy(dict(slug="x"))
    assert complete and [j["id"] for j in jobs] == ["a1", "b2"]
    assert jobs[0]["title"] == "Treasury Analyst" and jobs[0]["posted"] == "2026-10-20"
    assert jobs[0]["location"] == "New York, NY; London; Remote"
    assert jobs[1]["location"] == "Hong Kong" and jobs[1]["posted"] == ""
    assert request is not None


def test_breezy_non_list_response_is_empty():
    with mock.patch.object(js, "http", fake_http({"/json": {"error": "not found"}})):
        assert js.breezy(dict(slug="x")) == ([], True)


def test_breezy_is_registered_and_requires_slug():
    assert js.FETCH["breezy"] is js.breezy
    assert vc.REQUIRED_FIELDS["breezy"] == ("slug",)


JOBVITE_PAGE = """<table><tbody>
<tr><td class="jv-job-list-name"><a href="/acme/job/o1AbC">Treasury &amp; Liquidity Analyst</a></td>
<td class="jv-job-list-location">

   New York,
   New York

</td></tr>
<tr><td class="jv-job-list-name"><a href="/acme/job/o1AbC">Treasury &amp; Liquidity Analyst</a></td>
<td class="jv-job-list-location">New York, New York</td></tr>
<tr><td class="jv-job-list-name"><a href="/acme/job/o2XyZ">Quant</a></td>
<td class="jv-job-list-location">Remote</td></tr>
</tbody></table>"""


def test_jobvite_rows_dedupe_and_location_whitespace():
    with mock.patch.object(js, "http", fake_http({"/acme/jobs": JOBVITE_PAGE})):
        jobs, complete = js.jobvite(dict(company="acme"))
    assert complete and [j["id"] for j in jobs] == ["o1AbC", "o2XyZ"]
    assert jobs[0]["title"] == "Treasury & Liquidity Analyst"
    assert jobs[0]["location"] == "New York, New York"
    assert jobs[0]["url"] == "https://jobs.jobvite.com/acme/job/o1AbC"


def test_jobvite_unknown_company_page_has_no_rows():
    with mock.patch.object(js, "http", fake_http({"/zzz/jobs": "<html>Job Seeker FAQs</html>"})):
        assert js.jobvite(dict(company="zzz")) == ([], True)
    assert js.FETCH["jobvite"] is js.jobvite and vc.REQUIRED_FIELDS["jobvite"] == ("company",)


ISOLVED_PAGE = "<script>componentData: { organizationId : 1499, domainId : 1880, getParams : {} }</script>"


def test_isolvedhire_reads_domain_id_then_json_list():
    rows = {"data": {"jobs": [
        dict(id=11, title=" Loan Officer ", city="Clinton", abbreviation="NJ", startDateRef="Oct 20, 2026", workplaceType="Onsite"),
        dict(id=11, title="dup", city="X"),
        dict(id=12, title="Analyst", city="", stateName="New Jersey", startDateRef="bad", workplaceType="Remote"),
    ]}}
    routes = {"/core/jobs/1880": rows, "/jobs/": ISOLVED_PAGE}
    with mock.patch.object(js, "http", fake_http(routes)):
        jobs, complete = js.isolvedhire(dict(host="x.isolvedhire.com"))
    assert complete and [j["id"] for j in jobs] == ["11", "12"]
    assert jobs[0]["title"] == "Loan Officer" and jobs[0]["location"] == "Clinton, NJ"
    assert jobs[0]["posted"] == "2026-10-20" and jobs[0]["url"] == "https://x.isolvedhire.com/jobs/11.html"
    assert jobs[1]["posted"] == "" and jobs[1]["location"] == "New Jersey | Remote"


def silkroad_page(*ids):
    return "".join(
        f'<a id="Jobs_PagedJobList_Job-{i}" class="sr-panel" href="/CO/Careers/jobs/{i}">'
        f'<div class="sr-panel__title" id="t{i}"> Risk Analyst {i} </div>'
        f'<div class="sr-panel__location"><span class="sr-panel__meta">Dayton, OH</span></div></a>' for i in ids)


def test_silkroad_pages_until_no_new_rows_and_dedupes():
    pages = {"page=1": silkroad_page(1, 2), "page=2": silkroad_page(2, 3), "page=3": silkroad_page(3)}
    with mock.patch.object(js, "http", fake_http(pages)):
        jobs, complete = js.silkroad(dict(portal="CO/Careers"))
    assert complete and [j["id"] for j in jobs] == ["1", "2", "3"]
    assert jobs[0]["title"] == "Risk Analyst 1" and jobs[0]["location"] == "Dayton, OH"
    assert jobs[0]["url"] == "https://jobs.silkroad.com/CO/Careers/jobs/1"


def test_silkroad_page_cap_marks_incomplete():
    pages = {f"page={n}": silkroad_page(n) for n in range(1, 4)}
    with mock.patch.object(js, "http", fake_http(pages)):
        jobs, complete = js.silkroad(dict(portal="CO/Careers", max_pages=3))
    assert not complete and len(jobs) == 3


JOBAPS_PAGE = """<table><tbody>
<tr><th><a href="/MD/sup/bulpreview.asp?R1=00&amp;R2=PR0000&amp;R3=001" class="JobTitle">Practice Application</a></th></tr>
<tr class="even"><th><a href="https://www.jobapscloud.com/MD/sup/bulpreview.asp?b=&amp;R1=26&amp;R2=003492&amp;R3=0003" class="JobTitle" title="x">ACCOUNTANT II </a><br>
<a href="https://www.jobapscloud.com/MD/sup/bulpreview.asp?b=&amp;R1=26&amp;R2=003492&amp;R3=0003" class="JobNum" title="x">26-003492-0003 </a></th>
<td class="Locs">Baltimore City<br> </td></tr>
<tr><th><a href="https://www.jobapscloud.com/MD/sup/bulpreview.asp?b=&amp;R1=26&amp;R2=003492&amp;R3=0003" class="JobTitle">dup</a>
<a href="u" class="JobNum">26-003492-0003</a></th></tr>
</tbody></table>"""


def test_jobaps_skips_unnumbered_rows_dedupes_and_unescapes_url():
    with mock.patch.object(js, "http", fake_http({"/MD/": JOBAPS_PAGE})):
        jobs, complete = js.jobaps(dict(agency="MD"))
    assert complete and len(jobs) == 1 and jobs[0]["id"] == "26-003492-0003"
    assert jobs[0]["title"] == "ACCOUNTANT II" and jobs[0]["location"] == "Baltimore City"
    assert "&amp;" not in jobs[0]["url"] and "R2=003492" in jobs[0]["url"]
    assert js.FETCH["silkroad"] is js.silkroad and js.FETCH["jobaps"] is js.jobaps
    assert vc.REQUIRED_FIELDS["silkroad"] == ("portal",) and vc.REQUIRED_FIELDS["jobaps"] == ("agency",)


APPLICANTSTACK_PAGE = """<table id="data-table"><thead><tr><th>Title</th><th>Department</th><th>Location</th></tr></thead><tbody>
<tr class="oddrow"><td><a href="https://x.applicantstack.com/x/detail/a2cc40n1">Financial &amp; Risk Analyst</a></td><td>Finance</td><td>Washington, DC</td></tr>
<tr class="evenrow"><td><a href="https://x.applicantstack.com/x/detail/a2cc40n1">dup</a></td><td>Finance</td><td>X</td></tr>
<tr class="evenrow"><td><a href="https://x.applicantstack.com/x/detail/a2cc40n2">Quant</a></td><td>Risk</td><td>Remote</td></tr>
</tbody></table><div class="tablepager">Showing 1 - 2 of 2</div>"""


def test_applicantstack_reads_location_by_header_and_dedupes():
    with mock.patch.object(js, "http", fake_http({"/x/openings": APPLICANTSTACK_PAGE})):
        jobs, complete = js.applicantstack(dict(host="x.applicantstack.com"))
    assert complete and [j["id"] for j in jobs] == ["a2cc40n1", "a2cc40n2"]
    assert jobs[0]["title"] == "Financial & Risk Analyst" and jobs[0]["location"] == "Washington, DC"
    assert jobs[1]["location"] == "Remote"


def test_applicantstack_partial_pager_incomplete_empty_board_and_registered():
    partial = APPLICANTSTACK_PAGE.replace("Showing 1 - 2 of 2", "Showing 1 - 2 of 9")
    with mock.patch.object(js, "http", fake_http({"/x/openings": partial})):
        assert js.applicantstack(dict(host="x.applicantstack.com"))[1] is False
    with mock.patch.object(js, "http", fake_http({"/x/openings": "<html>Job Board Not Found</html>"})):
        assert js.applicantstack(dict(host="x.applicantstack.com")) == ([], True)
    assert js.FETCH["applicantstack"] is js.applicantstack and vc.REQUIRED_FIELDS["applicantstack"] == ("host",)


def test_committed_filters_drop_non_finance_associate_titles_but_keep_finance_ones():
    import json
    from pathlib import Path
    filters = json.loads((Path(js.__file__).parent / "data/config.json").read_text(encoding="utf-8"))["filters"]

    def kept(title):
        return js.matches(dict(title=title, location="New York, NY"), filters)

    for noisy in ("PSYCHOLOGY ASSOCIATE DOCTORATE, CORRECTIONAL", "Sales Strategy Associate Director",
                  "Associate, Strategic Communications", "Learning & Development Associate",
                  "Associate Director, Application & Product Architecture", "Associate General Manager"):
        assert not kept(noisy), noisy
    for wanted in ("Associate Director, Financial Planning", "Credit Risk Associate", "Associate, Asset Management",
                   "Senior Fixed Income Analyst", "Quantitative Researcher", "Treasury Operations & Reporting Analyst"):
        assert kept(wanted), wanted


def test_isolvedhire_page_without_domain_id_is_empty_and_registered():
    with mock.patch.object(js, "http", fake_http({"/jobs/": "<html>no id</html>"})):
        assert js.isolvedhire(dict(host="x.isolvedhire.com")) == ([], True)
    assert js.FETCH["isolvedhire"] is js.isolvedhire and vc.REQUIRED_FIELDS["isolvedhire"] == ("host",)

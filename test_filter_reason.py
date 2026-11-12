"""title_filter_reason names the rule behind every title rejection that matches() makes, and filtered rows skip roles
that were never candidates or are hidden by location."""
import json
import job_scout as js

F = json.load(open("data/config.json", encoding="utf-8"))["filters"]
TITLES = [
    "Treasury Associate", "Marketing Manager, SVP", "Lead Data Scientist, Credit Risk",
    "Capital Markets Operations Specialist II", "Experience Design Vice President - Agentic Treasury",
    "Warehouse Associate", "Senior Software Engineer", "Quantitative Developer, Rates", "Equity Research Associate",
    "Quantitative Trading & Research - Securities Services, Payments and CIB Treasury - Associate",
    "Securities Services Operations Analyst", "Summer Analyst Spring 2027 Risk",
]


def test_reason_agrees_with_matches():
    for t in TITLES:
        job = {"title": t, "location": "New York, NY"}
        reason = js.title_filter_reason(job, F)
        if reason:
            assert not js.matches(job, F), t
        elif any(js.contains_any(t, F["title_include"]) for _ in [0]):
            assert js.matches(job, F), t


def test_names_the_rule():
    assert js.title_filter_reason({"title": "Treasury Managing Director"}, F) == ("title_exclude", "managing director")
    assert js.title_filter_reason({"title": "Treasury Associate"}, F) is None


def test_no_include_hit_is_not_reported():
    assert js.title_filter_reason({"title": "Warehouse Forklift Operator"}, F) is None


def test_jpmorgan_quant_treasury_securities_services_kept():
    job = {"title": "Quantitative Trading & Research - Securities Services, Payments and CIB Treasury - Associate", "location": "New York, NY, United States"}
    assert js.matches(job, F)
    ops = {"title": "Securities Services Operations Analyst - Risk", "location": "New York, NY, United States"}
    assert not js.matches(ops, F)


def test_filtered_rows_skip_hits_and_location_hidden():
    jobs = [
        {"id": "1", "title": "Treasury Associate", "location": "New York, NY", "link": "a"},
        {"id": "2", "title": "Treasury Associate, Managing Director", "location": "New York, NY", "link": "b"},
        {"id": "3", "title": "Treasury Associate, Managing Director", "location": "Mumbai, India", "link": "c"},
        {"id": "4", "title": "Warehouse Forklift Operator", "location": "New York, NY", "link": "d"},
    ]
    hits = [j for j in jobs if js.matches(j, F)]
    rows, total = js._filtered_rows(jobs, hits, F)
    assert [r[0] for r in rows] == ["2"] and total == 1
    assert rows[0][4] == "title_exclude"


def test_audit_flags_core_titles_once():
    import filter_audit as fa
    data = {"companies": {"Citi": {"n": 2, "rows": [
        ["1", "Treasury Transformation Manager", "New York", "u1", "title_exclude", "transformation"],
        ["2", "Associate Banker", "Boston", "u2", "title_exclude", "associate banker"]]}}}
    flagged, counts = fa.audit(data, set())
    assert [f[1][0] for f in flagged] == ["1"] and counts == {"transformation": 1, "associate banker": 1}
    assert not fa.audit(data, {"Citi:1"})[0][0][3]


def test_loosened_rules_keep_core_titles_and_still_hide_others():
    kept = [
        "Associate, Fund Services - Portfolio Analytics", "Associate, Treasury, Global Banking Operations",
        "Treasury & Client Services Associate", "Senior Lead Validator - Cybersecurity Model Risk",
        "Senior FP&A Analyst - Supply Chain Finance", "Balance Sheet & Pricing Management Product Manager, SVP",
        "Securitized Products Market Risk Senior Vice President",
    ]
    hidden = ["Fund Services Operations Associate", "Cybersecurity Analyst", "Client Services Associate", "Marketing Manager, SVP"]
    for t in kept:
        assert js.matches({"title": t, "location": "New York, NY"}, F), t
    for t in hidden:
        assert not js.matches({"title": t, "location": "New York, NY"}, F), t

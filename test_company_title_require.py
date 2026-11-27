import job_scout as js

AMAZON = {"name": "Amazon", "title_require": ["treasury", "financial analyst"]}


def test_company_without_rule_keeps_everything():
    assert js.company_title_ok({"name": "Other"}, "Lead Associate")


def test_company_rule_keeps_only_required_titles():
    assert js.company_title_ok(AMAZON, "Treasury Manager, Treasury Strategy & Markets")
    assert js.company_title_ok(AMAZON, "Senior  Financial   Analyst, Pharmacy Finance")
    assert not js.company_title_ok(AMAZON, "Lead Associate")
    assert not js.company_title_ok(AMAZON, "Finance Manager, AWS Agentic AI")


def test_drop_removes_only_new_rows_that_fail_the_rule():
    queue = [
        {"company": "Amazon", "role": "Lead Associate", "state": "new"},
        {"company": "Amazon", "role": "Lead Associate", "state": "applied"},
        {"company": "Amazon", "role": "Treasury Manager", "state": "new"},
        {"company": "Other", "role": "Lead Associate", "state": "new"},
    ]
    counts = {}
    js._drop_company_title_filtered(queue, [AMAZON, {"name": "Other"}], counts)
    assert counts["company_title_dropped"] == 1
    assert [(r["company"], r["state"]) for r in queue] == [("Amazon", "applied"), ("Amazon", "new"), ("Other", "new")]

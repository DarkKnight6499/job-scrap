"""Lab, patient-safety, drug-manufacturing and similar non-finance titles are excluded; finance titles that share words stay."""
import json
import pytest
import job_scout as js

FILTERS = json.load(open("data/config.json", encoding="utf-8"))["filters"]


def _job(title):
    return {"title": title, "location": "New York, NY"}


@pytest.mark.parametrize("title", [
    "Quality Lab Associate II",
    "Associate Director, Global Patient Safety (GPS)",
    "Associate Manufacturing - Bulk Drug Substance Downstream - Days",
    "Associate Specialist Drug Safety",
    "Associate Medical Technologist",
    "Medical Business Associate - Delhi",
    "Associate, Packaging",
    "Associate Director, Vaccine Manufacturing",
    "Associate Director, Pharmacovigilance- FSP",
])
def test_science_titles_excluded(title):
    assert not js.matches(_job(title), FILTERS)


@pytest.mark.parametrize("title", [
    "Senior Financial Analyst - Manufacturing Finance",
    "Data Quality Associate",
    "Equity Research Senior Associate - Building Products, Paper & Packaging",
    "US Corporates - Credit Analyst, Director - Industrials & Transportation (Diversified Manufacturing)",
    "Sr Manager, Group Medical Underwriting",
    "Treasury Analyst",
])
def test_finance_titles_with_shared_words_stay(title):
    assert not js.contains_any(title, FILTERS["title_exclude"])

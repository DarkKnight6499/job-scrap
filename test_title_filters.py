"""Branch-side banking titles are excluded; adjacent finance roles stay."""
import json
import pytest
import job_scout as js

FILTERS = json.load(open("data/config.json", encoding="utf-8"))["filters"]


def _job(title):
    return {"title": title, "location": "New York, NY"}


@pytest.mark.parametrize("title", [
    "Branch Operations Associate Manager Palms",
    "Senior Branch Premier Banker Capital District",
    "Associate Branch Manager, Ameriprise Financial Advisors - Richmond, VA",
    "CA WA Private Mortgage Banking Associate Manager",
    "Client Experience Associate - Piedmont Market",
    "Registered Client Associate",
    "J.P. Morgan Wealth Management - Private Client Investment Associate - Bethesda",
    "Client Associate",
    "Associate Roadway Engineer",
    "Souscripteur adjoint ou souscriptrice adjointe, Équipe rationalisation de portefeuille",
    "Associate Auto Claims Adjuster Upskill",
    "Associate Territory Manager, Neurovascular - Dallas, TX",
    "Associate Relationship  Banker - Austin Central Market - Austin ,TX",
])
def test_branch_titles_excluded(title):
    assert not js.matches(_job(title), FILTERS)


@pytest.mark.parametrize("title", [
    "Mortgage Quantitative Analyst, VP",
    "Corporate Banker - Global Capital Management - Vice President",
    "Retail Credit Risk Expert",
    "Treasury Associate",
    "Credit Risk Review Analyst",
])
def test_adjacent_roles_kept(title):
    f = dict(FILTERS, title_include=[])
    assert js.matches(_job(title), f)

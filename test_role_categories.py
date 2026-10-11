"""Offline tests for the category chips on the queue page (ROLE_CATEGORIES in job_scout.py).

The page decides category membership in JavaScript (see _EXTRA_SCRIPT): the role text is
lowercased and tested against one regex per category, '\\b(?:term1|term2|...)\\b' with each term
regex-escaped. A row may match several categories; a row matching none gets the key 'other'.
categories_for() below is a Python reimplementation of exactly that logic. It reads the category
list from _extra_script() (the same JSON the page embeds), so the terms under test are the
terms the page ships.
"""
import json
import re

import job_scout


def _page_categories():
    script = job_scout._extra_script()
    start = script.index("var CATEGORIES = ") + len("var CATEGORIES = ")
    end = script.index(";\n", start)
    return json.loads(script[start:end])


_CATS = _page_categories()
_RES = {
    c["label"]: re.compile(r"\b(?:" + "|".join(re.escape(t.lower()) for t in c["terms"]) + r")\b")
    for c in _CATS if c["terms"]
}


def categories_for(role):
    """Labels matched by a role title, or ['Other (no category)'] when none match (mirrors the JS)."""
    text = role.lower()
    hits = [label for label, rx in _RES.items() if rx.search(text)]
    return hits or ["Other (no category)"]


NEW_CATEGORY_SAMPLES = {
    "Commercial Banking/Lending": (
        ["Relationship Banking Associate", "Special Assets Associate I", "Commercial Credit Senior Associate, CRE"],
        ["Financial Analyst", "Associate Actuary", "Software Developer"],
    ),
    "Portfolio Manager/Analyst": (
        ["Commercial Portfolio Manager II", "Associate Portfolio Manager - Parametric", "Portfolio Analyst"],
        ["Project Manager", "Product Manager", "Senior Accountant"],
    ),
    "Accounting/Controllership/Tax": (
        ["Senior Accountant", "External Reporting Controller Senior Associate", "Tax Manager"],
        ["Financial Analyst", "Budget Analyst", "Account Manager"],
    ),
    "Financial Analysis/Business Finance": (
        ["Senior Financial Analyst", "Finance Business Partner", "Budget Analyst Associate"],
        ["Senior Accountant", "Credit Analyst", "Business Analyst"],
    ),
    "Compliance/Financial Crime": (
        ["AML and Sanction Associate", "Senior Compliance Associate", "KYC Analyst"],
        ["Operating Risk Manager", "Credit Analyst", "Financial Planner"],
    ),
    "Audit/Assurance": (
        ["Associate, Internal Audit", "Risk Assurance Senior Associate", "Senior Audit Manager"],
        ["Quality Assurance Engineer", "Accounting Manager", "Compliance Associate"],
    ),
    "Risk Management (General)": (
        ["Risk Manager", "Senior Risk Analyst", "First Line Risk and Control Associate Director"],
        ["Portfolio Manager", "Credit Analyst", "Software Developer"],
    ),
    "Trading/Markets/Derivatives": (
        ["Associate Trader, CMO Derivatives Desk", "Trade Support Analyst", "Equity Derivatives Structuring Associate"],
        ["Financial Analyst", "Relationship Manager", "Accounting Associate"],
    ),
    "Structured Finance/Securitization": (
        ["Structured Finance - Asset-Backed Securities, Analyst", "Structured Credit & CLOs, Senior Analyst",
         "Associate, Securitization"],
        ["Financial Analyst", "Commercial Portfolio Manager", "Equity Research Analyst"],
    ),
    "M&A/Advisory": (
        ["Mergers & Acquisitions Senior Associate", "IB Associate - Energy", "Associate (Financial Restructuring)"],
        ["Internal Audit Associate", "Product Manager", "Credit Analyst"],
    ),
    "Research/Investment Analysis": (
        ["Senior Research Analyst", "Investment Analyst, Public Credit", "Credit Research Analyst"],
        ["Financial Analyst", "Risk Analyst", "Operations Associate"],
    ),
    "Quant/Modeling/Pricing": (
        ["Quantitative Research Analyst", "Senior Associate - Valuation", "Quant Developer"],
        ["Financial Analyst", "Software Developer", "Credit Analyst"],
    ),
    "Operations/Servicing": (
        ["Derivatives Operations Analyst", "Associate, FX Settlements", "Lead Client Servicing Associate"],
        ["Financial Analyst", "Strategy Associate", "Operating Risk Manager"],
    ),
    "Technology/Data": (
        ["Software Engineer", "Data Engineer II", "Cyber Security Manager"],
        ["Financial Analyst", "Credit Officer", "Wealth Advisor"],
    ),
    "Product Management": (
        ["Senior Product Manager - Travel", "Associate, Product Management", "Product Owner, Payments Platform"],
        ["Product Controller", "Project Manager", "Marketing Associate"],
    ),
    "Strategy/Consulting": (
        ["Corporate Strategy Associate", "Senior Consultant", "Management Consulting Analyst"],
        ["Financial Analyst", "Operations Associate", "Tax Manager"],
    ),
    "Program/Project/Business Analysis": (
        ["Senior Business Analyst", "Program Manager, Transformation", "Project Management Office Lead (PMO)"],
        ["Financial Analyst", "Product Manager", "Project Finance Associate"],
    ),
    "Sales/Business Development": (
        ["Business Development Representative", "Institutional Sales Associate", "Investor Relations Analyst"],
        ["Financial Analyst", "Credit Analyst", "Salesforce Administrator"],
    ),
    "Retail/Consumer Banking": (
        ["Associate Community Banker", "Branch Manager", "Retail Management Associate"],
        ["Financial Analyst", "Commercial Credit Analyst", "Portfolio Manager"],
    ),
    "Wealth/Trust/Advisory": (
        ["Wealth Advisor", "Trust Administrator Associate", "Private Bank Associate"],
        ["Financial Analyst", "Operations Associate", "Commercial Banker"],
    ),
    "Payments/Cash Management": (
        ["Payments Sales Senior Associate", "Cash Management Officer", "Merchant Services Analyst"],
        ["Financial Analyst", "Credit Analyst", "Treasury Analyst"],
    ),
    "Insurance/Actuarial": (
        ["Associate Actuary", "Property Claims Manager", "Insurance Associate"],
        ["Financial Analyst", "Credit Analyst", "Risk Manager"],
    ),
    "Legal/HR/Marketing/Admin": (
        ["Communications Associate", "Corporate Counsel", "Executive Assistant"],
        ["Financial Analyst", "Credit Analyst", "Software Developer"],
    ),
    "Early Careers/Internships": (
        ["Summer Analyst Program", "2027 Associate Development Program", "Software Engineering Intern"],
        ["Senior Financial Analyst", "Vice President, Credit", "Director of Operations"],
    ),
    "Energy/Construction/Infrastructure": (
        ["Associate, Construction Finance", "IB Associate - Energy", "Project Finance Associate Lead"],
        ["Financial Analyst", "Credit Analyst", "Wealth Advisor"],
    ),
}

# Existing categories: their terms are unchanged, sample titles must keep mapping to them.
EXISTING_SAMPLES = {
    "Treasury/ALM/Liquidity": "Treasury Analyst",
    "FP&A": "Senior Associate, FP&A",
    "Credit Risk": "Credit Risk Analyst",
    "Fixed Income": "Fixed Income Sales Associate",
    "Operational Risk": "Operational Risk Manager",
    "Quantitative Risk/Analytics": "Quantitative Analyst",
    "Capital Planning": "CCAR Capital Planning Lead",
    "Model Risk/Validation": "Model Validation Associate",
    "Market Risk": "Market Risk Analyst",
    "Capital Markets/IB": "Capital Markets Associate",
    "Risk & Controls (General)": "Technology Risk Manager",
    "Portfolio/Asset Management": "Asset Management Analyst",
    "Underwriting": "Underwriting Associate",
    "Real Estate Finance": "Commercial Real Estate Analyst",
    "Private Equity/Credit": "Private Credit Associate",
    "Wealth Management": "Wealth Management Associate",
    "Equity Research": "Equity Research Associate",
    "Corporate Finance/Development": "Corporate Development Associate",
}


def test_every_new_category_is_covered_by_a_sample():
    original = set(EXISTING_SAMPLES)
    labels = {c["label"] for c in _CATS if c["terms"]}
    assert labels - original == set(NEW_CATEGORY_SAMPLES)


def test_new_categories_sit_after_existing_and_before_other():
    labels = [c["label"] for c in _CATS]
    assert labels[-1] == "Other (no category)"
    assert labels[: len(EXISTING_SAMPLES)] == list(EXISTING_SAMPLES)


def test_new_category_representative_titles_match():
    for label, (hits, _misses) in NEW_CATEGORY_SAMPLES.items():
        for title in hits:
            assert label in categories_for(title), (label, title, categories_for(title))


def test_new_category_near_misses_do_not_match():
    for label, (_hits, misses) in NEW_CATEGORY_SAMPLES.items():
        for title in misses:
            assert label not in categories_for(title), (label, title, categories_for(title))


def test_existing_category_samples_still_map():
    for label, title in EXISTING_SAMPLES.items():
        assert label in categories_for(title), (label, title, categories_for(title))


def test_existing_terms_are_unchanged():
    # Spot-check that the original term lists were not edited while adding categories.
    assert job_scout.ROLE_CATEGORIES["Underwriting"] == ["underwriting"]
    assert job_scout.ROLE_CATEGORIES["Fixed Income"] == ["fixed income"]
    assert job_scout.ROLE_CATEGORIES["Equity Research"] == ["equity research"]


def test_unmatched_title_stays_in_other():
    assert categories_for("Zamboni Driver") == ["Other (no category)"]
    assert categories_for("Chief Happiness Wizard") == ["Other (no category)"]


def test_multiple_categories_allowed():
    cats = categories_for("Credit Risk Manager")
    assert "Credit Risk" in cats and "Risk Management (General)" in cats

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
# Optional per-category exclusion phrases (same matching): a title containing one never joins the category.
_EXCL = {
    c["label"]: re.compile(r"\b(?:" + "|".join(re.escape(t.lower()) for t in c["exclude"]) + r")\b")
    for c in _CATS if c.get("exclude")
}


def categories_for(role):
    """Labels matched by a role title, or ['Other (no category)'] when none match (mirrors the JS)."""
    text = role.lower()
    hits = [label for label, rx in _RES.items()
            if rx.search(text) and not (label in _EXCL and _EXCL[label].search(text))]
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


# Terms tightened from bare words to multi-word terms (or excluded): each entry is
# (label, titles that must still match, near-miss titles that must not match that label).
TIGHTENED = [
    ("Technology/Data",
     ["Software Engineer", "Applied AI/ML Senior Associate", "Associate Director, Lead Cloud Architect",
      "Staff IT Analyst II", "Business Intelligence Developer", "Associate Director Engineering"],
     ["IT Risk Associate II", "AI Risk Management Manager", "Equity Research Associate - Medical Technology",
      "Technology Risk Management Specialist", "Credit Risk Strategy and Analytics", "Senior FP&A Analyst - Public Cloud",
      "Portfolio Construction & Risk, Senior Analyst", "Senior Risk Engineer", "Investment Banking Associate - Technology M&A"]),
    ("Strategy/Consulting",
     ["Corporate Strategy Associate", "Associate Manager, Strategy & Operations", "Oliver Wyman Senior Consultant",
      "Senior Associate, Strategic Initiatives", "Associate, Growth Strategy"],
     ["Credit Risk Strategy Manager I", "Portfolio Strategy Analyst", "Treasury Management Consultant III",
      "Trading Strategy Associate", "Regional Consultant - US Wealth", "Strategic Analytics - Senior Associate"]),
    ("Commercial Banking/Lending",
     ["Corporate Banker", "Commercial Banking Associate", "Portfolio Banker - Middle Market"],
     ["Investment Banking Associate - Equity Capital Markets", "Investment Banking and Capital Markets-Banker-Healthcare Analyst",
      "Global Lead, Commercial Risk Strategy"]),
    ("Retail/Consumer Banking",
     ["Retail Management Associate", "Deposit Operations Associate", "Retail Loan Credit Risk Reviewer"],
     ["Investment & Corporate Banking - Consumer & Retail, Associate", "Equity Research Associate, Food Retail",
      "Deposit & Liquidity Product Management", "Senior Lead Product Manager, Deposit Pricing"]),
    ("Wealth/Trust/Advisory",
     ["Wealth Advisor", "Private Wealth Associate", "Trust Administrator Associate", "Trust Portfolio Manager"],
     ["Asset & Wealth Management, Investment Risk Management, Equities, Associate", "Quant Analytics Senior Associate - Trust & Security",
      "Equity Research Associate - Trust Banks"]),
    ("Legal/HR/Marketing/Admin",
     ["Corporate Communications Associate", "Legal Due Diligence Associate", "Brand Strategy Associate"],
     ["Investment Banking Associate - Media & Communications", "Legal Entity Controller Associate",
      "Quantitative Analytics Associate - Co-brand Card Data Analytics", "Credit Analyst, Director (Legal)"]),
    ("Energy/Construction/Infrastructure",
     ["Associate, Construction Lending", "Power & Utilities Investment Banking Associate", "Coordinator Capital Projects I"],
     ["Portfolio Construction Analyst", "Air Power Defense Portfolio Director"]),
    ("Payments/Cash Management",
     ["Merchant Services Analyst", "Card Solutions Product Manager"],
     ["Senior Business Analyst - Card Loss Forecasting", "Merchant Banking Associate", "Merchant Strategy & Operations"]),
    ("Program/Project/Business Analysis",
     ["Implementation Manager", "Senior Implementation Consultant"],
     ["Analyst, Portfolio Implementation", "Director, Implementation Risk"]),
    ("Sales/Business Development",
     ["Distribution Sales Associate"],
     ["Distribution Center Associate", "Associate Director, Warehousing & Distribution"]),
    ("Trading/Markets/Derivatives",
     ["Equity Derivatives Structuring Associate", "Equity Structuring Associate"],
     ["Corporate Structuring Associate", "Alternatives Fund Structuring, Associate"]),
    ("M&A/Advisory",
     ["Leveraged Finance Associate", "Capital Solutions Group Vice President", "Debt Capital Market Analyst"],
     ["Leveraged Finance Portfolio Manager", "Credit Analyst - Leveraged Finance Portfolio Management"]),
]

# Known false positives fixed with the exclusion list or rewording.
def test_operations_research_is_not_research_analysis():
    assert "Research/Investment Analysis" not in categories_for("Operations Research Analyst, Senior")
    assert "Research/Investment Analysis" in categories_for("Senior Research Analyst")


def test_valuation_controller_is_not_quant():
    assert "Quant/Modeling/Pricing" not in categories_for("Valuation Controller - Vice President - Rates")
    assert "Accounting/Controllership/Tax" in categories_for("Valuation Controller - Vice President - Rates")
    assert "Quant/Modeling/Pricing" in categories_for("Senior Associate - Valuation")


def test_floor_broker_stays_in_trading():
    assert "Trading/Markets/Derivatives" in categories_for("Equities Electronic Trading - J.P. Morgan NYSE Floor Broker - Associate")


def test_tightened_terms_keep_positives_and_reject_near_misses():
    for label, hits, misses in TIGHTENED:
        for title in hits:
            assert label in categories_for(title), (label, title, categories_for(title))
        for title in misses:
            assert label not in categories_for(title), (label, title, categories_for(title))


def test_bare_ambiguous_terms_are_gone():
    # These bare words matched far too broadly; only multi-word forms are allowed now.
    bare = {
        "Technology/Data": {"it", "ai", "analytics", "technology", "engineering", "cloud", "architect", "engineer"},
        "Strategy/Consulting": {"strategy", "strategic", "consultant"},
        "Retail/Consumer Banking": {"retail", "deposit", "deposits"},
        "Wealth/Trust/Advisory": {"wealth", "trust"},
        "Legal/HR/Marketing/Admin": {"legal", "communications", "content", "brand"},
        "Energy/Construction/Infrastructure": {"power", "construction"},
        "Payments/Cash Management": {"card", "merchant"},
        "Commercial Banking/Lending": {"banker", "banking associate", "commercial risk"},
        "Program/Project/Business Analysis": {"implementation"},
        "Sales/Business Development": {"distribution"},
        "Trading/Markets/Derivatives": {"structuring"},
        "M&A/Advisory": {"leveraged finance"},
    }
    for label, words in bare.items():
        assert not words & set(job_scout.ROLE_CATEGORIES[label]), label


def test_new_family_terms_catch_leftover_titles():
    cases = {
        "Portfolio Manager/Analyst": ["Portfolio Construction Analyst", "Portfolio Strategy Analyst"],
        "Risk Management (General)": ["Risk Analysis Specialist II", "Vice President, Risk / Policy Management", "Capital Risk Vice President"],
        "Operations/Servicing": ["Associate, Derivative Operations", "Loan Funding Specialist", "Associate - Investor Services"],
        "Financial Analysis/Business Finance": ["Director, Capital Controls & Planning", "Finance Control Manager - Senior Associate"],
        "M&A/Advisory": ["Vice President, Capital Solutions"],
        "Insurance/Actuarial": ["Health & Benefits Consultant, Senior Associate"],
        "Technology/Data": ["Data Protection Associate", "Data Visualization Senior Associate"],
    }
    for label, titles in cases.items():
        for title in titles:
            assert label in categories_for(title), (label, title, categories_for(title))


def test_treasury_sales_roles_are_sales_not_treasury():
    for title in ["Treasury Management Sales Officer Senior", "Sales Executive, Treasury Solutions",
                  "Pre-Sales Solution Specialist, Treasury & Payments (Quantum)", "Director, Treasury Management Sales"]:
        hits = categories_for(title)
        assert "Sales/Business Development" in hits and "Treasury/ALM/Liquidity" not in hits, (title, hits)
    # real treasury roles keep their chip
    for title in ["Treasury Analyst", "Treasury Manager", "Liquidity Risk Manager, Treasury"]:
        assert "Treasury/ALM/Liquidity" in categories_for(title), title

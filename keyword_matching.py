"""keyword_matching.py - word-boundary, alias-aware term matching.

Fixes a real bug: plain substring matching (`term in text`) false-positives
on short terms that appear inside unrelated words - e.g. "intern" inside
"Internal"/"International", or "engineer" inside "Financial Engineer" (as an
exclude term, that would wrongly reject the role). Word-boundary matching
(via lookaround, not \\b, so it also works correctly for terms that start or
end with punctuation) eliminates that class of false positive.

Also normalizes a curated set of full-phrase <-> acronym aliases, so a JD
that spells out "Asset Liability Management" matches "ALM", and vice versa.
"""
import re

ALIASES = {
    "asset liability management": "alm",
    "asset-liability management": "alm",
    "funds transfer pricing": "ftp",
    "liquidity coverage ratio": "lcr",
    "net stable funding ratio": "nsfr",
    "high quality liquid assets": "hqla",
    "high-quality liquid assets": "hqla",
    "value at risk": "var",
    "economic value of equity": "eve",
    "net interest income": "nii",
    "risk-weighted assets": "rwa",
    "risk weighted assets": "rwa",
    "internal capital adequacy assessment process": "icaap",
    "counterparty credit risk": "ccr",
    "credit valuation adjustment": "cva",
    "value-at-risk": "var",
}


def _term_variants(term):
    key = term.lower()
    variants = {term}
    if key in ALIASES:
        variants.add(ALIASES[key])
    else:
        for full, short in ALIASES.items():
            if key == short:
                variants.add(full)
    return variants


def _pattern(term):
    escaped = re.escape(term)
    return re.compile(r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])", re.IGNORECASE)


def contains_term(text, term):
    return any(_pattern(v).search(text) for v in _term_variants(term))

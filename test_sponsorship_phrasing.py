"""Sponsorship classifier: wording that excludes OPT too is Blocked, plain no-sponsorship wording is "OPT Only", benign look-alikes stay Unclear."""
import pytest

import job_scout as js

BLOCKED = [
    "USAA does not provide visa sponsorship for this role. Please do not apply for this role if at any time (now or in the future) you will need immigration support (i.e., H-1B, TN, STEM OPT Training Plans, etc.).",
    "Candidates requiring immigration support, such as H-1B or STEM OPT, will not be considered.",
    "This role is limited to persons with an indefinite right to work in the United States.",
]
OPT_ONLY = [
    "We do not provide visa sponsorship for this role.",
    "We are unable to provide immigration support for this role.",
    "This role is not eligible for visa assistance.",
    "No immigration support is available for this position.",
    "Applicants must be authorized to work in the U.S. without current or future employer support.",
    "We will not sponsor now or in the future.",
]
HARD_BLOCKED = [
    "Must be a U.S. citizen. We do not sponsor visas.",
    "Active secret clearance required. No sponsorship.",
    "This position is subject to ITAR. We cannot sponsor.",
    "No sponsorship available. Candidates on STEM OPT or CPT are not considered.",
]
UNCLEAR = [
    "We offer visa sponsorship and immigration support for qualified candidates.",
    "Immigration support is available for the right candidate.",
    "Do not apply unless you are passionate about risk management.",
    "Please do not apply through third parties; use our careers site.",
    "Candidates must be eligible to work in the United States. We will sponsor H-1B visas where appropriate.",
    "The role supports our immigration law practice clients and does not require travel.",
    "Authorized to work in the U.S. is required; we provide relocation assistance.",
]


@pytest.mark.parametrize("text", BLOCKED)
def test_blocks_immigration_support_phrasing(text):
    assert js.classify_sponsorship(text)[0] == "Blocked"


@pytest.mark.parametrize("text", HARD_BLOCKED)
def test_hard_blocks_stay_blocked(text):
    assert js.classify_sponsorship(text)[0] == "Blocked"


@pytest.mark.parametrize("text", OPT_ONLY + [
    "Please do not apply for this role if you will need immigration support (H-1B, TN).",
    "We do not offer relocation or immigration assistance.",
])
def test_plain_no_sponsorship_is_opt_only(text):
    assert js.classify_sponsorship(text)[0] == "OPT Only"


@pytest.mark.parametrize("text", UNCLEAR)
def test_benign_lookalikes_stay_unclear(text):
    assert js.classify_sponsorship(text)[0] == "Unclear"


def test_us_abbreviation_before_space_is_normalized():
    assert js.classify_sponsorship("Candidates must be authorized to work in the U.S. without sponsorship now or in the future.")[0] == "OPT Only"

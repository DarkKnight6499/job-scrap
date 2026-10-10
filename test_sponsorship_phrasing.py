"""Sponsorship classifier: immigration-support phrasing (USAA 2026-10-03) blocks, benign look-alikes do not."""
import pytest

import job_scout as js

BLOCKED = [
    "USAA does not provide visa sponsorship for this role. Please do not apply for this role if at any time (now or in the future) you will need immigration support (i.e., H-1B, TN, STEM OPT Training Plans, etc.).",
    "Please do not apply for this role if you will need immigration support (H-1B, TN).",
    "Candidates requiring immigration support, such as H-1B or STEM OPT, will not be considered.",
    "We are unable to provide immigration support for this role.",
    "This role is not eligible for visa assistance.",
    "Applicants must be authorized to work in the U.S. without current or future employer support.",
    "No immigration support is available for this position.",
    "We do not offer relocation or immigration assistance.",
    "This role is limited to persons with an indefinite right to work in the United States.",
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


@pytest.mark.parametrize("text", UNCLEAR)
def test_benign_lookalikes_stay_unclear(text):
    assert js.classify_sponsorship(text)[0] == "Unclear"


def test_us_abbreviation_before_space_is_normalized():
    assert js.classify_sponsorship("Candidates must be authorized to work in the U.S. without sponsorship now or in the future.")[0] == "Blocked"

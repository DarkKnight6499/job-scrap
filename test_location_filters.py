"""locations_exclude: non-US sites stay out, US sites (including ones that say Headquarters) stay in."""
import json
from pathlib import Path

import pytest

import job_scout as js

CFG = json.loads((Path(__file__).parent / "data" / "config.json").read_text(encoding="utf-8"))["filters"]
FILTERS = {"title_include": [], "title_exclude": [], "locations_include": [], "locations_exclude": CFG["locations_exclude"]}


def kept(loc):
    return js.matches({"title": "x", "location": loc}, FILTERS)


EXCLUDED = [
    "Headquarters", "Central Region (City Area)", "One Island East", "Kwun Tong, HK", "Central, HK",
    "Monterrey, NLE, MX", "Citi Center - MX", "Bukit Jalil KL, MY", "Schweiz - Nordschweiz", "Lodz, PL, 93-281",
    "Las Condes, RM, CL", "Port Moresby, PG", "Ramat-Gan, ISR", "ED3 - 20 Brandon Street, Edinburgh",
    "Whitby, Ontario", "Ontario Home Office", "Perth Office", "Guangzhou", "Gandhi Nagar - GIFT City",
]
US_KEPT = [
    "US-NJ-Princeton-100-Headquarters", "Office - USA - CA - Headquarters", "New York, NY", "Ontario, CA",
    "Houston Office", "Jersey City", "Charlotte", "Boston, Massachusetts", "San Francisco Office",
    "Washington D.C.", "Montclair, NJ", "Nashville, Tennessee", "San Juan, Puerto Rico", "Cameron", "Wilmington, Delaware",
]


@pytest.mark.parametrize("loc", EXCLUDED)
def test_non_us_locations_excluded(loc):
    assert not kept(loc)


@pytest.mark.parametrize("loc", US_KEPT)
def test_us_locations_kept(loc):
    assert kept(loc)


def test_drop_location_filtered_removes_only_non_us_new_rows():
    queue = [
        {"id": "a", "state": "new", "location": "Kwun Tong"},
        {"id": "b", "state": "new", "location": "US-NJ-Princeton-100-Headquarters"},
        {"id": "c", "state": "applied", "location": "Kwun Tong"},
    ]
    counts = {}
    js._drop_location_filtered(queue, CFG, counts)
    assert [e["id"] for e in queue] == ["b", "c"]
    assert counts["location_dropped"] == 1

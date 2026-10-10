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
    "PH", "MX", "SG", "Headquarters", "Central Region (City Area)", "One Island East", "Kwun Tong, HK", "Central, HK",
    "Monterrey, NLE, MX", "Citi Center - MX", "Bukit Jalil KL, MY", "Schweiz - Nordschweiz", "Lodz, PL, 93-281",
    "Las Condes, RM, CL", "Port Moresby, PG", "Ramat-Gan, ISR", "ED3 - 20 Brandon Street, Edinburgh",
    "Whitby, Ontario", "Pasig - 4th Floor JMT Corporate Condominium", "Delhi NCR", "Munich - Müllerstrasse", "Fortitude Valley, QLD, au", "Ludhiana", "Kaohsiung", "Camana Bay", "Tunis", "København S, DK, 2300", "Saint Helier, Jersey", "Newcastle, NSW, au", "Ontario Home Office", "Perth Office", "Guangzhou", "Gandhi Nagar - GIFT City",
    "Perth, WA, au", "Docklands, AU", "1/124, SHIVAJI GARDENS, MOONLI", "Thane, IN", "AU - BRISBANE - 50 JAMES ST", "Perth - 225 St. Georges Terrace", "Norwich - Willow", "Parma - Via Carlo Pisacane, 1B", "Łódź, PL, 93-281", "Belfast - Merchant Square (IE)", "Belfast - 20 Adelaide Street",
    "Heredia, Provincia de Heredia", "Boadilla del Monte", "2 Locations (MesseTurm DEFAMESS HQ)", "Købanhavn K, DK, 1402", "3 Locations (Zrich)", "5 Locations (Canadian Head Office)", "2 Locations (POL Gdynia 3T Office Park Tower C)", "2 Locations (Lvis)", "Kulim, Kedah, al", "Milton Keynes", "CD - Kinshasa, Democratic Republic of Congo", "Northampton, Barclays Campus, Pavilion Drive", "Henley-on-Thames, Oxfordshire", "Ranjangaon", "2 Locations (9 10 TAUNUSANLAGE FRANKFURT AM MAIN)", "1054, Retiro, Capital Federal", "Kolhapur", "Jersey, JE", "Brunei, BN", "Addlestone", "Calvin Klein Halfweg Sugar City", "Birmingham, One Snow Hill", "Myanmar, MM - AIA Myanmar", "Pathum Tani", "I-Think Techno, Kanjurmarg", "PSA - Tandag City",
]
US_KEPT = [
    "IN", "DE", "CA", "ID", "AR", "Indianapolis, IN",
    "US-NJ-Princeton-100-Headquarters", "Office - USA - CA - Headquarters", "New York, NY", "Ontario, CA",
    "Houston Office", "Jersey City", "Charlotte", "Boston, Massachusetts", "San Francisco Office",
    "Washington D.C.", "Montclair, NJ", "Nashville, Tennessee", "San Juan, Puerto Rico", "Cameron", "Wilmington, Delaware", "Perth Amboy, NJ", "Nassau County, NY", "Brisbane, CA", "Belfast, ME", "Delhi, NY", "Medina, OH",
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

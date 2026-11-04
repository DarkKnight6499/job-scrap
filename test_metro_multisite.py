"""NY metro filter on the hosted page: one qualifying site in a multi-site row keeps it visible."""
import pytest

import job_scout as js

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


def row(i, location):
    return dict(id=f"Acme:{i}", company="Acme", role=f"Risk Analyst {i}", location=location, link=f"https://example.com/{i}",
                source="test", state="new", first_seen="2026-11-01", posted="2026-11-01", kw_hits=[],
                sponsorship_status="Allowed", sponsorship_evidence="", closed_on=None)


def test_metro_box_uses_any_site_segment(tmp_path):
    rows = [row(1, "Albany, NY; New York, NY"), row(2, "Albany, NY"), row(3, "Buffalo, NY | Jersey City, NJ"), row(4, "Chicago, IL")]
    page_path = tmp_path / "index.html"
    js.write_queue_html(rows, str(page_path))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(page_path.as_uri())
        page.locator("#metroOnlyBox").check()
        hidden = page.evaluate("() => Array.from(document.querySelectorAll('table tbody tr')).filter(tr => tr.classList.contains('hidden-by-location')).map(tr => tr.textContent.match(/Risk Analyst [0-9]/)[0])")
        browser.close()
    assert sorted(hidden) == ["Risk Analyst 2", "Risk Analyst 4"]

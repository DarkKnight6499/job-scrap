"""Offline tests for the opt-in partial read on throttled Eightfold boards (Microsoft 429s on bursts)."""
import urllib.error
from unittest import mock

import pytest

import job_scout as js

TERMS = ["treasury", "liquidity", "risk", "audit", "fp&a"]


def _err():
    return urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)


def _company(**extra):
    return dict(name="Acme", ats="eightfold", host="h.example", domain="acme.com", pcsx=True,
                term_workers=1, _terms=TERMS, **extra)


def _fake(failing):
    """Page fake: the empty-term probe reports a huge board (so the whole-board read is skipped), each term returns one job."""
    def page(c, term, start):
        if term == "":
            return {"count": 99999, "positions": []}
        if term in failing:
            raise _err()
        return {"count": 1, "positions": [{"id": f"id-{term}", "name": f"Role {term}", "locations": ["NYC"]}]}
    return page


def test_default_board_still_fails_whole_on_one_error():
    with mock.patch.object(js, "_eightfold_page", _fake({"liquidity"})):
        with pytest.raises(urllib.error.HTTPError):
            js.eightfold(_company())


def test_partial_ok_keeps_rows_and_marks_incomplete():
    with mock.patch.object(js, "_eightfold_page", _fake({"liquidity"})):
        jobs, complete = js.eightfold(_company(partial_ok=True))
    assert sorted(j["id"] for j in jobs) == ["id-audit", "id-fp&a", "id-risk", "id-treasury"]
    assert complete is False


def test_partial_ok_breaker_stops_after_consecutive_failures():
    calls = []
    inner = _fake({"liquidity", "risk", "audit"})

    def page(c, term, start):
        calls.append(term)
        return inner(c, term, start)
    with mock.patch.object(js, "_eightfold_page", page):
        jobs, complete = js.eightfold(_company(partial_ok=True, max_term_failures=3))
    assert [j["id"] for j in jobs] == ["id-treasury"] and complete is False
    assert "fp&a" not in calls  # stopped after three failed terms in a row


def test_partial_ok_all_terms_failing_raises():
    with mock.patch.object(js, "_eightfold_page", _fake(set(TERMS))):
        with pytest.raises(urllib.error.HTTPError):
            js.eightfold(_company(partial_ok=True))


def test_time_budget_stops_cleanly_and_marks_incomplete():
    clock = iter([0, 0, 1000, 1000, 1000, 1000, 1000, 1000])
    with mock.patch.object(js, "_eightfold_page", _fake(set())), mock.patch.object(js.time, "time", lambda: next(clock)):
        jobs, complete = js.eightfold(_company(time_budget_s=10))
    assert len(jobs) < len(TERMS) and complete is False


def test_clean_read_is_complete():
    with mock.patch.object(js, "_eightfold_page", _fake(set())):
        jobs, complete = js.eightfold(_company(partial_ok=True))
    assert len(jobs) == len(TERMS) and complete is True

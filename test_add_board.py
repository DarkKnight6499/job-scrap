"""add_board: a hand-found board is added only after a live check returns jobs, never twice, and the tried-log records it."""
import json

import add_board


def _setup(tmp_path):
    cfg = tmp_path / "config.json"
    log = tmp_path / "log.json"
    cfg.write_text(json.dumps({"companies": [{"name": "Acme", "ats": "adp_rm", "domain": "acmecareers"}]}, indent=1) + "\n", encoding="utf-8")
    return cfg, log


def _fetch_ok(entry):
    return [{"title": "Analyst", "location": "New York, NY"}], True


def test_adds_verified_board_and_logs_it(tmp_path):
    cfg, log = _setup(tmp_path)
    ok, msg, jobs = add_board.add_board(cfg, log, "Beta Insurance, Inc.", "adp_rm", {"domain": "betacareers"}, _fetch_ok, "2026-11-23", aliases=("Beta Insurance",))
    assert ok and len(jobs) == 1
    names = [c["name"] for c in json.loads(cfg.read_text(encoding="utf-8"))["companies"]]
    assert names == ["Acme", "Beta Insurance, Inc."]
    logged = json.loads(log.read_text(encoding="utf-8"))
    assert logged["betainsuranceinc"]["found"] == "adp_rm betacareers" and "betainsurance" in logged


def test_refuses_duplicate_name_empty_board_and_shared_endpoint(tmp_path):
    cfg, log = _setup(tmp_path)
    assert not add_board.add_board(cfg, log, "ACME", "adp_rm", {"domain": "other"}, _fetch_ok, "2026-11-23")[0]
    assert not add_board.add_board(cfg, log, "Gamma", "adp_rm", {"domain": "gammacareers"}, lambda e: ([], True), "2026-11-23")[0]
    assert not add_board.add_board(cfg, log, "Delta", "adp_rm", {"domain": "acmecareers"}, _fetch_ok, "2026-11-23")[0]
    assert not log.exists()  # nothing written on a refusal


def test_dry_run_writes_nothing(tmp_path):
    cfg, log = _setup(tmp_path)
    before = cfg.read_text(encoding="utf-8")
    ok, msg, _ = add_board.add_board(cfg, log, "Epsilon", "adp_rm", {"domain": "epsiloncareers"}, _fetch_ok, "2026-11-23", dry_run=True)
    assert ok and cfg.read_text(encoding="utf-8") == before and not log.exists()

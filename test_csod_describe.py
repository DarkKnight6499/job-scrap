import job_scout as j


def test_csod_describe_reuses_list_desc(monkeypatch):
    calls = []

    def fake(c):
        calls.append(c["corp"])
        return [dict(id="1", desc="alpha"), dict(id="2", desc="beta")], True

    j._CSOD_DESC.clear()
    monkeypatch.setattr(j, "csod", fake)
    c = {"ats": "csod", "corp": "x", "site": 1}
    assert j.describe(c, {"id": "2"})[0] == "beta"
    assert j.describe(c, {"id": "1"})[0] == "alpha"
    assert j.describe(c, {"id": "9"})[0] == ""
    assert calls == ["x"]

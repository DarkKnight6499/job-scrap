"""ukg_ready pages job-requisitions and builds rows (offline, http stubbed)."""
import job_scout


def test_ukg_ready_rows_and_paging(monkeypatch):
    pages = {0: {"job_requisitions": [{"id": 1, "job_title": " Senior ALM Analyst ", "job_description": "<p>Manage ALM</p>",
                                       "location": {"city": "Waltham", "state": "MA"}, "is_remote_job": False}],
                 "_paging": {"offset": 0, "size": 50, "total": 1}}}
    monkeypatch.setattr(job_scout, "http", lambda url, **kw: pages[int(url.split("offset=")[1].split("&")[0])])
    rows, ok = job_scout.ukg_ready({"board": "https://secure7.saashr.com/ta/6206516.careers?ein_id=118955345&lang=en-US"})
    assert ok and len(rows) == 1
    assert rows[0]["title"] == "Senior ALM Analyst" and rows[0]["location"] == "Waltham, MA"
    assert rows[0]["url"] == "https://secure7.saashr.com/ta/6206516.careers?ShowJob=1&ein_id=118955345"
    assert "Manage ALM" in rows[0]["desc"]

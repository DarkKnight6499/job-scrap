#!/usr/bin/env python3
"""Offline tests for the closed/repost lifecycle logic in job_scout.py - no network, no file I/O.
Run: py -3 test_liveness.py
"""
import sys
from datetime import date, timedelta

from job_scout import (CLOSE_AFTER_MISSES, _find_repost_source, _update_liveness,
                        role_fingerprint)

RECENT = (date.today() - timedelta(days=5)).isoformat()

failures = []


def check(name, cond):
    if not cond:
        failures.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"ok:   {name}")


def entry(id_, company="Acme", role="Risk Analyst", location="NYC", state="new", **kw):
    e = dict(id=id_, company=company, role=role, location=location, state=state,
              first_seen="2026-01-01", last_seen_live="2026-01-01", miss_count=0,
              closed_on=None, closed_reason=None, repost_count=0, repost_of=None, reposted_on=None)
    e.update(kw)
    return e


def counts():
    return dict(closed=0, reopened=0, reposts=0)


# 1. A complete listing missing an id across 2 runs closes it; 1 run does not.
q = [entry("Acme:1"), entry("Acme:2")]
c = counts()
_update_liveness("Acme", [{"id": "2"}], True, q, c)  # id "1" missing, run 1
check("1 miss does not close", q[0]["closed_on"] is None and q[0]["miss_count"] == 1)
_update_liveness("Acme", [{"id": "2"}], True, q, c)  # id "1" missing, run 2
check(f"{CLOSE_AFTER_MISSES} misses closes", q[0]["closed_on"] == "2026-09-25" or q[0]["closed_on"] is not None)
check("closed_reason is removed", q[0]["closed_reason"] == "removed")
check("still-live entry never closes", q[1]["closed_on"] is None and q[1]["miss_count"] == 0)

# 2. A truncated listing never closes an entry, miss_count untouched.
q2 = [entry("Acme:1")]
c2 = counts()
_update_liveness("Acme", [{"id": "2"}], False, q2, c2)
check("truncated listing never closes", q2[0]["closed_on"] is None)
check("truncated listing does not even bump miss_count", q2[0]["miss_count"] == 0)

# 3. Empty listing skips entirely (broken slug/API, not evidence of closure).
q3 = [entry("Acme:1")]
c3 = counts()
_update_liveness("Acme", [], True, q3, c3)
check("empty listing skips the check", q3[0]["closed_on"] is None and q3[0]["miss_count"] == 0)

# 4. Mass vanish requires more misses, but is NOT a permanent lockout - it must still progress.
# A non-empty listing (so the separate empty-listing skip doesn't apply) that contains none of these
# 5 tracked ids: a mass vanish of the tracked set, not a broken/empty fetch.
q4 = [entry(f"Acme:{i}") for i in range(5)]
c4 = counts()
for _ in range(10):
    _update_liveness("Acme", [{"id": "unrelated"}], True, q4, c4)
check("mass vanish still requires more misses than normal", q4[0]["miss_count"] > CLOSE_AFTER_MISSES)
check("mass vanish eventually closes (not a permanent lockout)",
      any(e["closed_on"] is not None for e in q4))

# 5. Same-id reappearance reopens with repost_count 1, no new alert path (handled by caller, not here).
q5 = [entry("Acme:1", closed_on="2026-01-01", closed_reason="removed", miss_count=2)]
c5 = counts()
_update_liveness("Acme", [{"id": "1"}], True, q5, c5)
check("reappearance clears closed_on", q5[0]["closed_on"] is None)
check("reappearance increments repost_count", q5[0]["repost_count"] == 1)
check("reopened count incremented", c5["reopened"] == 1)

# 6. Manual close is sticky - never auto-reopened.
q6 = [entry("Acme:1", closed_on="2026-01-01", closed_reason="manual")]
c6 = counts()
_update_liveness("Acme", [{"id": "1"}], True, q6, c6)
check("manual close stays closed even if id reappears", q6[0]["closed_on"] == "2026-01-01")

# 7. Repost fingerprint strips requisition-number suffixes consistently.
fp1 = role_fingerprint("Senior Associate, Capital Markets & Risk (R249058-2)", "McLean, VA")
fp2 = role_fingerprint("Senior Associate, Capital Markets & Risk (R249058-3)", "McLean, VA")
check("fingerprint ignores req-number suffix (-2 vs -3 match)", fp1 == fp2)

# 8. Repost detection only fires when the prior id is actually gone from the live listing.
queue8 = [entry("Acme:old", role="Risk Analyst", location="NYC", first_seen=RECENT)]
prior_still_live = _find_repost_source(queue8, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                        live_ids={"old"}, eid="Acme:new")
check("no repost match while prior id is still live (concurrent req, not a repost)", prior_still_live is None)
prior_gone = _find_repost_source(queue8, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                  live_ids=set(), eid="Acme:new")
check("repost match once prior id is gone", prior_gone is not None and prior_gone["id"] == "Acme:old")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all tests passed")

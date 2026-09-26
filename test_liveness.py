#!/usr/bin/env python3
"""Offline tests for the closed/repost lifecycle logic in job_scout.py - no network, no file I/O.
Run: py -3 test_liveness.py
"""
import sys
from datetime import date, timedelta

from job_scout import (CLOSE_AFTER_MISSES, _classify_ghosts, _find_repost_source, _update_liveness,
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
queue8 = [entry("Acme:old", role="Risk Analyst", location="NYC", first_seen=RECENT, last_seen_live=RECENT)]
prior_still_live = _find_repost_source(queue8, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                        live_ids={"old"}, eid="Acme:new", claimed=set())
check("no repost match while prior id is still live (concurrent req, not a repost)", prior_still_live is None)
prior_gone = _find_repost_source(queue8, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                  live_ids=set(), eid="Acme:new", claimed=set())
check("repost match once prior id is gone", prior_gone is not None and prior_gone["id"] == "Acme:old")

# 9. The repost window is measured from when the prior went quiet (closed_on/last_seen_live), not
# from when it was first seen - a long-lived posting that just recently closed must still link.
OLD = (date.today() - timedelta(days=400)).isoformat()  # first_seen well outside REPOST_WINDOW_DAYS
queue9 = [entry("Acme:longlived", role="Risk Analyst", location="NYC", first_seen=OLD,
                 last_seen_live=RECENT, closed_on=RECENT)]
recently_closed = _find_repost_source(queue9, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                       live_ids=set(), eid="Acme:new", claimed=set())
check("old first_seen does not block a match when the prior closed recently",
      recently_closed is not None and recently_closed["id"] == "Acme:longlived")

queue9b = [entry("Acme:longgone", role="Risk Analyst", location="NYC", first_seen=RECENT, closed_on=OLD)]
stale_close = _find_repost_source(queue9b, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                   live_ids=set(), eid="Acme:new", claimed=set())
check("a prior closed long ago does not match even with a recent-looking first_seen", stale_close is None)

# 10. Two fresh postings sharing a fingerprint in the same run must not both claim the same prior.
queue10 = [entry("Acme:shared", role="Risk Analyst", location="NYC", first_seen=RECENT, last_seen_live=RECENT)]
claimed10 = set()
first_claim = _find_repost_source(queue10, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                   live_ids=set(), eid="Acme:new1", claimed=claimed10)
claimed10.add(first_claim["id"])
second_claim = _find_repost_source(queue10, "Acme", role_fingerprint("Risk Analyst", "NYC"),
                                    live_ids=set(), eid="Acme:new2", claimed=claimed10)
check("a prior already claimed this run is not matched again", second_claim is None)

# 11. Ghost classification: 3+ serial appearances spanning 120+ days is "Likely"; anything short
# of that (too few, too short a span, or overlapping/non-serial) stays "Unclear", never a false
# "not a ghost" - same one-directional caution as sponsorship classification.
def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()

queue11 = [
    entry("Acme:g1", role="Ghost Role", location="NYC", first_seen=days_ago(200), closed_on=days_ago(170)),
    entry("Acme:g2", role="Ghost Role", location="NYC", first_seen=days_ago(150), closed_on=days_ago(100)),
    entry("Acme:g3", role="Ghost Role", location="NYC", first_seen=days_ago(60)),
]
_classify_ghosts(queue11)
check("3 serial postings spanning 120+ days is flagged Likely",
      all(e["ghost_status"] == "Likely" for e in queue11))
check("ghost evidence is populated when flagged", bool(queue11[0]["ghost_evidence"]))

queue11b = [
    entry("Acme:h1", role="Fresh Role", location="NYC", first_seen=days_ago(200), closed_on=days_ago(170)),
    entry("Acme:h2", role="Fresh Role", location="NYC", first_seen=days_ago(150)),
]
_classify_ghosts(queue11b)
check("only 2 appearances (below GHOST_MIN_COUNT) stays Unclear",
      all(e["ghost_status"] == "Unclear" for e in queue11b))

queue11c = [
    entry("Acme:i1", role="Overlap Role", location="NYC", first_seen=days_ago(30), closed_on=days_ago(20)),
    entry("Acme:i2", role="Overlap Role", location="NYC", first_seen=days_ago(25)),  # starts before i1 closed
    entry("Acme:i3", role="Overlap Role", location="NYC", first_seen=days_ago(10)),
]
_classify_ghosts(queue11c)
check("non-serial (overlapping) postings stay Unclear even with 3+ appearances",
      all(e["ghost_status"] == "Unclear" for e in queue11c))

queue11d = [
    entry("Acme:j1", role="Quick Cycle", location="NYC", first_seen=days_ago(50), closed_on=days_ago(45)),
    entry("Acme:j2", role="Quick Cycle", location="NYC", first_seen=days_ago(40), closed_on=days_ago(35)),
    entry("Acme:j3", role="Quick Cycle", location="NYC", first_seen=days_ago(30)),
]
_classify_ghosts(queue11d)
check("3+ serial postings under the 120-day span stay Unclear",
      all(e["ghost_status"] == "Unclear" for e in queue11d))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all tests passed")

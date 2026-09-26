# job-scrap

Free job discovery: polls public ATS job-board APIs (Greenhouse, Lever, Ashby,
SmartRecruiters, Workday, Oracle Recruiting Cloud) for a curated list of
companies, filters by title/location keywords, flags explicit no-sponsorship
language in the description, and pushes a phone alert (ntfy) for anything
new. No paid scraping API, no LLM tokens.

Runs on a schedule via GitHub Actions (`.github/workflows/scout.yml`, every 30 min)
- no server or laptop required. State (`data/state.json`, `data/queue.json`)
is committed back to the repo after each run so the next run picks up where
the last one left off.

Every run also diffs each company's live listing against its queued entries:
a posting missing for two consecutive complete (unpaginated-cap) runs is
marked closed, a same-id reappearance is reopened, and a new id matching a
closed entry's (title, location) is recorded as a repost of it - alerting
again only if the prior posting had been auto-blocked and the new one isn't.

**No application-tracker integration.** Unlike a private companion version,
this build doesn't know what you've already applied to.

## Setup

1. Add a repo secret `NTFY_TOPIC` (Settings -> Secrets and variables ->
   Actions) with your own [ntfy.sh](https://ntfy.sh) topic string. Subscribe
   to that topic in the ntfy phone app to receive alerts. The topic is
   deliberately not committed to `data/config.json` since this repo is
   public - anyone who saw it in the repo could subscribe to your alerts.
2. Edit `data/config.json` - `companies` (each needs `ats` +
   the fields that ATS needs: `slug` for greenhouse/ashby/smartrecruiters,
   `host`/`tenant`/`site`/`search` for workday, `host`/`site`/`search` for
   oracle) and `filters` (`title_include`/`title_exclude`/`locations_include`).
3. Trigger a manual run from the Actions tab (`workflow_dispatch`) to confirm
   it works before waiting for the schedule.

## Sharing the queue (GitHub Pages)

Every scheduled run also regenerates `docs/index.html` and commits it, so the
same queue can be shared with someone else via a stable URL instead of
sending them a file. One-time setup: repo Settings -> Pages -> Source:
"Deploy from a branch" -> branch `main`, folder `/docs`. The page includes a
live client-side search box (comma = OR, space = AND) so different people
can filter the same shared data down to their own interests - nothing is
sent anywhere and each browser remembers only its own last search.

## Local use

```
python job_scout.py --check      # per-company job/match counts, no state change
python job_scout.py --dry-run    # show what would queue/alert, no state change
python job_scout.py              # normal run
python job_scout.py --queue      # show the triage queue
python job_scout.py --queue --all --html queue.html   # write the same page written to docs/index.html
```

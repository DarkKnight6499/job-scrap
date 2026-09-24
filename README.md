# job-scrap

Free job discovery: polls public ATS job-board APIs (Greenhouse, Lever, Ashby,
SmartRecruiters, Workday, Oracle Recruiting Cloud) for a curated list of
companies, filters by title/location keywords, flags explicit no-sponsorship
language in the description, and pushes a phone alert (ntfy) for anything
new. No paid scraping API, no LLM tokens.

Runs on a schedule via GitHub Actions (`.github/workflows/scout.yml`, every 2
hours) - no server or laptop required. State (`data/state.json`,
`data/queue.json`) is committed back to the repo after each run so the next
run picks up where the last one left off.

**No application-tracker integration.** Unlike a private companion version,
this build doesn't know what you've already applied to, so it can re-alert on
a posting that drops out of a company's search window and reappears later.

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

## Local use

```
python job_scout.py --check      # per-company job/match counts, no state change
python job_scout.py --dry-run    # show what would queue/alert, no state change
python job_scout.py              # normal run
python job_scout.py --queue      # show the triage queue
```

# JD proxy (Cloudflare Worker)

Backs the Copy JD button on the public Jobs Tracker page. Stateless: the page sends the ATS details
for one posting, the Worker fetches its description from the employer's public job API and returns
plain text. Free tier is 100k requests/day.

Deploy once:

    cd worker
    npx wrangler login
    npx wrangler deploy

Copy the printed `https://job-scout-jd-proxy.<subdomain>.workers.dev` URL into `JD_PROXY_URL` in
`job_scout.py`, then regenerate the page.

Only these hosts are ever fetched: boards-api.greenhouse.io, api.lever.co, api.eu.lever.co,
api.ashbyhq.com, api.smartrecruiters.com, `*.myworkdayjobs.com`, `*.oraclecloud.com`.

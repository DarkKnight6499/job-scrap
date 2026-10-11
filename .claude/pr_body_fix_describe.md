## describe(): return full descriptions for ATS types that came back empty

Queue rows store no description, so the local "Copy JD" button relies on `describe()` fetching it again. These ATS types fell through to `return "", "", ""`. Each now has one `describe()` branch using plain HTTP via `http()` and `strip_html()`, capped at 30000 chars.

| ATS | Source used |
|---|---|
| deshaw | job page `__NEXT_DATA__` `jobData.jobDescription` (JSON-LD has only the intro paragraph, 467 vs 1966 chars on req 5886) |
| hrmdirect | detail page body text minus script/style/nav/header/footer |
| goldman | GraphQL `role(externalSourceId: <numeric id from the job URL>) { descriptionHtml }` |
| ukg | the detail page embeds the opportunity JSON; the longest `"Description"` string is the full HTML text |
| brassring | same cookie + RFT session as `brassring()`, then the page's own `POST /TgNewUI/Search/Ajax/JobDetails`; textarea answers form the body |
| bpce | `wp/v2/job?slug=<url slug>` gives the WP post id (our id is `job_number`, not the post id), then `bpce/v1/posts/?lang=en&_uid=job-<post_id>-x` `content.main.text` |
| lever | `api.lever.co/v0/postings/<slug>/<id>` (EU region host honoured), same field assembly as `lever()`. Not a closed-posting issue: live GMO rows were empty because the branch was missing |

### Before / after (text length in chars, 3 live queue rows per type, state new/opt_only/shortlisted)

| ATS (company) | Rows (id) | Before | After |
|---|---|---|---|
| deshaw (D. E. Shaw) | 5886, 6011, 5258 | 0, 0, 0 | 1966, 3859, 2316 |
| hrmdirect (Oppenheimer) | 3735679, 3742508, 3671054 | 0, 0, 0 | 3620, 4530, 4427 |
| goldman (Goldman Sachs) | 179299, 183255, 183413 | 0, 0, 0 | 3533, 3024, 3788 |
| ukg (Cathay Bank) | 7424f4e1..., 69c05866..., c72058c5... | 0, 0, 0 | 10388, 8210, 9145 |
| brassring (UBS) | 352008, 342483, 349505 | 0, 0, 0 | 4653, 5632, 7154 |
| bpce (Natixis) | 21963, 21881, 20890 | 0, 0, 0 | 7296, 4447, 3204 |
| lever (GMO) | 901dc25c..., 69fd389b... (de42e3c8... is 404) | 0, 0 | 6938, 5467 |

All first characters checked by eye: real job text (hrmdirect starts with the page title "... - Careers At Oppenheimer & Co. Inc." and a "Back To Openings" crumb, then the posting).

### Not fixable / ignored

- Lever GMO `de42e3c8-84f2-4059-8245-0b8a4b83dca1`: the posting API returns 404 and the row has miss_count 7, so it is closed. The other two GMO rows work.
- iCIMS MSCI 5700: HTTP 410 Gone, row miss_count 28, closed. Other iCIMS rows (Charles Schwab, MSCI 5997) return full text, so the existing branch is fine.
- Nothing else left empty: brassring and bpce both had plain-HTTP routes.

### Tests

New `test_describe_branches.py` (offline, `fake_http` style from `test_fetchers.py`, 13 tests) covers every new branch including the empty/missing paths (deshaw without `__NEXT_DATA__`, goldman null role, ukg without a Description, bpce unknown slug, url-less goldman rows). `python -m pytest -q`: 370 passed.

Only `describe()` plus one private helper (`_brassring_detail`) changed; no filters, list fetchers, state, docs or data files touched.

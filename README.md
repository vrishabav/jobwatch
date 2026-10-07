# jobwatch v2: free intern-role alerts (quant, AI labs, SWE, big tech)

Two GitHub Actions workflows poll ~130 firms' job boards, direct career sites, open job datasets and
aggregators, then alert you the moment a **new** intern / co-op / campus / off-cycle role appears
(any term, not just Summer 2027).

| Workflow | Cadence | Covers |
|---|---|---|
| `jobwatch` | every 30 min | Greenhouse (US **and EU**), Lever, Ashby, Workable, SmartRecruiters, Recruitee, Workday, Eightfold (Microsoft, Netflix), Amazon, Jane Street, SIG (iCIMS), Citadel (sitemap), Simplify feed, QuantRoles + Kadoa Quant datasets |
| `jobwatch-render` | every 3 h | JS-only career sites rendered in headless Chromium: Google, Meta, Apple, Citadel Securities, Two Sigma, Optiver, Mako |

## Setup (about 10 minutes)
1. Create a GitHub repo and push this folder to it.
   **Make it public** (nothing sensitive is stored: only job postings; secrets stay encrypted) to get unlimited
   free Actions minutes. If you keep it **private**, free minutes are 2,000/month: edit the two `cron:` lines
   to hourly (`7 * * * *`) and every 6 h (`23 */6 * * *`).
   ```bash
   git init -b main && git add . && git commit -m "jobwatch"
   gh repo create jobwatch --public --source . --push
   ```
2. Settings -> Actions -> General -> Workflow permissions -> **Read and write**.
3. Actions tab -> enable workflows -> run **jobwatch**, then **jobwatch-render** once each.
4. GitHub Settings -> Notifications: keep Email on for "Participating, @mentions". Each alert is an Issue that
   @-mentions you; that is what sends the email. Install the GitHub mobile app for push.
5. Optional channels (repo Settings -> Secrets and variables -> Actions): `NTFY_TOPIC` (free push via the ntfy app,
   no account), or `SMTP_USER` + `SMTP_PASS` (+ `SMTP_TO`) for Gmail with an App Password.

## First thing after the first runs: read `jobwatch/coverage.md`
Every source is listed as OK or NOT COVERED with the reason. I could not run this against the live sites from my
sandbox, so these are **unverified until your first run**: Greenhouse EU endpoint, Kadoa's raw `jobs.json` path/fields,
the Eightfold (Microsoft/Netflix) and Workday tenant endpoints, the Citadel sitemap pattern, and the link patterns
for the rendered sites (Google, Meta, Apple, Two Sigma, Optiver, Mako). Anything that fails shows up in coverage.md
and in one warning issue; fix the pattern in `config.json` or rely on the aggregators below for that firm.
Guessed slugs are identity-checked against Greenhouse's board name so a wrong guess cannot silently track another company.

## Tuning (`jobwatch/config.json`)
`keywords` / `exclude` / `locations` are regexes; `simplify.categories` picks Software / AI/ML/Data / Quant / Hardware / Product;
add firms under `companies` with slug guesses (pin with `"ats"`, use `"all": true` to merge several boards);
other sections: `workday`, `eightfold`, `html`, `sitemaps`, `render`, `feeds`.
Aggregator hits are de-duplicated against direct sources and labelled "(via ...)".

## Sign up for these too (free)
| Service | What it adds | Notes |
|---|---|---|
| **QuantRoles** (quantroles.com) | 410+ quant firms incl. "hidden gem" ones | Free weekly email by region; also feeds jobwatch via `openings.json` |
| **Kadoa Quant** (kadoa.com/quant) | ~2,900 roles, 71 quant firms, daily | CSV export; open MIT dataset at github.com/kadoa-org/quant-jobs (feeds jobwatch); no email alerts that I could confirm |
| **WallStreetQuants** (thewallstreetquants.com/jobs) | ~2,300 roles, 60+ firms, Intern/New Grad filter | Email is required to unlock the full list |
| **OpenQuant** (openquant.co + openquant.substack.com) | Quant board + large newsletter | Free |
| **TraderMath jobs** (tradermath.org/jobs) | ~1,400 roles, filters for firm/type/country | Free account for saved searches; email alerts unconfirmed |
| **HiringCafe** (hiring.cafe) | Search engine over company career pages (millions of jobs) | Free; daily email digest for saved searches (per a third-party review, I could not open the site) |
| **UptimeRobot job-page monitor** (uptimerobot.com/free-tools/job-alert-job-page-monitoring) | Email when any career page changes (Citadel, Google, Mako ... anything) | Free: 50 pages, 5-min checks; visual change detection, not keyword-based |
| **LinkedIn / Handshake / Indeed saved searches** | Catch custom sites | Standard feature: save a search for "intern" and turn on email alerts |

## Known gaps
- Firms with custom sites and no public feed rely on the rendered-page watcher, the aggregators, or UptimeRobot.
- Cloudflare or bot-protection can block GitHub's IP ranges for some sites (Citadel, Google); coverage.md will show it.
- Listings that exist only behind a login or application portal are not visible to any of this.

## Notes
- State is committed daily, which also stops GitHub pausing the schedule after 60 idle days.
- If no alert channel succeeds, state is not saved, so the role is retried next run.
- Reset: delete `jobwatch/state.json` and re-run (you'll get a fresh snapshot).

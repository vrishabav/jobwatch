# jobwatch v3: free intern alerts (quant, AI labs, big tech, SWE)

Runs by itself on GitHub Actions and emails you (via a GitHub Issue that @-mentions you) **once** for every
new intern / co-op / off-cycle / residency role, any term.

## What it watches
| Layer | Sources | Speed |
|---|---|---|
| Company boards (direct) | ~260 firms: 170 on verified boards (Greenhouse US+EU, Ashby, Lever, Workable, SmartRecruiters, Recruitee, Rippling), incl. separate campus/intern boards (CTC, Radix, DRW, Marshall Wace, Walleye, Maven …) and AI labs (OpenAI, Anthropic, Perplexity, Mistral, Cohere, xAI, Cursor, Cognition, ElevenLabs …); the rest are auto-resolved guesses | daily |
| Career sites (direct) | Amazon, Microsoft & Netflix (Eightfold), Millennium (Eightfold), NVIDIA, Salesforce, Intel, Qualcomm, Adobe, AMD, G-Research, Arrowstreet, PEAK6, Jain Global (Workday), SIG (iCIMS), Citadel, Citadel Securities, Optiver, D. E. Shaw, G-Research (sitemaps) | daily |
| JS-only sites (headless browser) | Meta, Two Sigma, Mako, Google (fallback) | daily (render workflow) |
| zshah101 registry | Every board in zshah101's ~5,000-employer list (Greenhouse, Ashby, Lever, Workday, Oracle, SmartRecruiters, Workable, Rippling…), polled by *your* engine with **no US/season limits**; tech-role filter (SWE / data / ML / quant / security) | daily |
| Big tech | Amazon, Microsoft, Netflix (APIs); NVIDIA, Salesforce, Intel, Qualcomm, Adobe, AMD (Workday); Google, Apple (career-page HTML); Meta, Google fallback (headless browser) | daily |
| Research | CERN (SmartRecruiters) + auto-resolved guesses for AI2, Simons/Flatiron, Isomorphic, Arc, CZI, Mila, Vector, EMBL, Turing Institute | daily |
| Aggregators | **Kadoa Quant** (77 quant firms, daily), **QuantRoles** (400+ quant firms), **zshah101 Intern Engine** (~5,000 employers' ATS feeds, every 30 min), **Simplify** (Summer + off-season list), **SpeedyApply** US + International tables | daily |

Aggregator copies of a role you were already alerted about are suppressed; alerts say "via …" when the role came from an aggregator.

## Guarantees
- **Only new roles.** Every role ID is remembered (for a year, or as long as it stays posted). A role alerts once, ever.
- **No floods.** A source's first successful run is a silent baseline; adding new firms never spams you with their existing roles. Dated postings older than `max_age_days` (21) never alert.
- **Never twice.** Roles are queued in `outbox.json`, the state is committed and pushed, and only then are alerts sent. If sending fails, they stay queued for the next run.
- **Automatic.** The `schedule:` in both workflows runs them on GitHub's clock; you never need to click Run. State is committed every run, so GitHub never pauses the schedule for inactivity.
- **Browse everything open:** `jobwatch/OPEN_ROLES.md` lists every currently open matching role (all sources, merged).

## Schedule
Both workflows run **once a day** (API 07:07 IST, browser 08:47 IST) — change the `cron:` line to adjust.
A daily run fits easily inside free minutes whether the repo is public or private.

## Checking health
- `jobwatch/coverage.md` shows every source and board as OK or NOT COVERED, with the reason.
- A broken source sends one "need attention" issue.
- To fix a firm, put its board as `"boards": ["greenhouse:<slug>"]` in `config.json`. You can read the slug off any job link: `job-boards.greenhouse.io/<slug>/…`, `jobs.ashbyhq.com/<slug>/…`, `jobs.lever.co/<slug>/…`.

## Tuning `jobwatch/config.json`
- `keywords` / `exclude` / `locations`: regexes. For example, `"locations": "India|Remote|Singapore|London|Hong Kong"` limits alerts to those places. Leave it empty for worldwide.
- `companies`: `{"name": "X", "boards": ["ashby:x"]}` (verified) or `{"name": "X", "slugs": ["x", "xai"]}` (auto-resolved with a name check).
- `workday`, `eightfold`, `sitemaps`, `render`, `feeds`, `markdown`, `simplify`: see the existing entries for the format.

## Optional channels (repo Settings → Secrets → Actions)
- `NTFY_TOPIC`: free phone push via the ntfy app.
- `SMTP_USER` + `SMTP_PASS` (+ `SMTP_TO`): send direct email through Gmail with an App Password.

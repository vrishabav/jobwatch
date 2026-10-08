# jobwatch v3.6: free intern alerts (quant, AI labs, big tech, SWE)

Runs by itself on GitHub Actions and emails you (via a GitHub Issue that @-mentions you) **once** for every
new intern / co-op / off-cycle / residency role, any term.

## What it watches
| Layer | Sources | Speed |
|---|---|---|
| Company boards (direct) | ~260 firms: 170 on verified boards (Greenhouse US+EU, Ashby, Lever, Workable, SmartRecruiters, Recruitee, Rippling), incl. separate campus/intern boards (CTC, Radix, DRW, Marshall Wace, Walleye, Maven …) and AI labs (OpenAI, Anthropic, Perplexity, Mistral, Cohere, xAI, Cursor, Cognition, ElevenLabs …); the rest are auto-resolved guesses | daily |
| Career sites (direct) | Amazon, Microsoft & Netflix (Eightfold), Millennium (Eightfold), NVIDIA, Salesforce, Intel, Qualcomm, Adobe, AMD, G-Research, Arrowstreet, PEAK6, Jain Global (Workday), Citadel, Citadel Securities, Optiver, D. E. Shaw, G-Research (sitemaps) | daily |
| JS-only sites (headless browser) | Meta, Google (fallback) | daily (render workflow) |
| zshah101 registry | Every board in zshah101's ~5,000-employer list (Greenhouse, Ashby, Lever, Workday, Oracle, SmartRecruiters, Workable, Rippling…), polled by *your* engine with **no US/season limits**; tech-role filter (SWE / data / ML / quant / security) | daily |
| Big tech | Amazon, Microsoft, Netflix (APIs); NVIDIA, Salesforce, Intel, Qualcomm, Adobe, AMD (Workday); Google, Apple (career-page HTML); AMD (Jibe API); Meta, Google fallback (headless browser) | daily |
| Research | CERN (SmartRecruiters) + auto-resolved guesses for AI2, Simons/Flatiron, Isomorphic, Arc, CZI, Mila, Vector, EMBL, Turing Institute | daily |
| Aggregators | **Kadoa Quant** (77 quant firms, daily), **QuantRoles** (400+ quant firms), **zshah101 Intern Engine** (~5,000 employers' ATS feeds, every 30 min), **Simplify** (Summer + off-season list), **SpeedyApply** US + International tables | daily |

**Filters:** quant firms, AI labs and CERN alert on *every* intern role. Everything else alerts only on SWE / data / ML / AI / quant / security internships, and the ~5,000 registry boards only on titles that literally say intern, internship or co-op. PhD/postdoc-only roles are excluded (remove `phd` from `exclude` to change). Two Sigma, SIG and Mistral come via Kadoa / QuantRoles / Simplify because their sites block automated readers.

The same role found in two places (company site and an aggregator, in either order) alerts once: matched by posting ID, then by company + title. Alerts say "via …" when the role came from an aggregator.

## Guarantees
- **Only new roles.** Every role ID is remembered (for a year, or as long as it stays posted). A role alerts once, ever.
- **No floods.** A source's first successful run is a silent baseline; adding new firms never spams you with their existing roles. Dated postings older than `max_age_days` (21) never alert. Editing the filters absorbs old roles that newly match silently; only postings from the last 2 days can alert.
- **Safe saves.** Each run checks out the latest state and never auto-merges conflicting state; a conflicting run stops without sending, and the next run redoes the work.
- **Never twice.** Roles are queued in `outbox.json`, the state is committed and pushed, and only then are alerts sent. If sending fails, they stay queued for the next run.
- **Automatic.** The `schedule:` in the workflow runs it on GitHub's clock; you never need to click Run. State is committed every run, so GitHub never pauses the schedule for inactivity.
- **Browse everything open:** `jobwatch/OPEN_ROLES.md` lists every currently open matching role (all sources, merged).

## Schedule
- GitHub triggers the `jobwatch` workflow every 4 hours, but the first job (`gate`, ~10 seconds) only checks the repo history: if the last completed run is **under 20 hours old, everything else is skipped**. So the real work (API poll, then the browser render for Meta/Google, in the same workflow) runs about once a day.
- Why every 4 hours and not once: GitHub's scheduler is best-effort. A trigger can start hours late or be skipped. With six chances a day, a late or missing one is simply covered by the next, and the roles are never lost.
- 20 hours (not 24) keeps the daily run time from creeping later each day.
- A run that failed (no commit) counts as not completed, so the next trigger retries it.
- **Run workflow** (manual) always runs everything, ignoring the check.
- Cost: about 14 min per real run plus ~1 min per check, roughly 600 min/month, inside a private repo's 2,000 free minutes.

## Checking health (30 seconds)
1. **Is it running?** `jobwatch/coverage.md` starts with a "Recent runs" table: start time (UTC; add 5:30 for IST), trigger (`schedule` = automatic, `workflow_dispatch` = you clicked), sources OK, new roles queued. Expect one `api` and one `render` row a day. A `jobwatch` run that shows only the `gate` job is a normal "not due yet" check.
2. **Did it alert?** A run with new roles opens an issue titled "N new intern role(s)…". No issue means nothing new matched.
3. **Is a source broken?** The same file lists every source as OK or NOT COVERED. A source failing three runs in a row sends one "need attention" issue.
4. **Actions tab:** a green tick on `jobwatch`; filter by Event = `schedule` to see automatic runs.
- To fix a firm, put its board as `"boards": ["greenhouse:<slug>"]` in `config.json`. You can read the slug off any job link: `job-boards.greenhouse.io/<slug>/…`, `jobs.ashbyhq.com/<slug>/…`, `jobs.lever.co/<slug>/…`.

## Tuning `jobwatch/config.json`
- `keywords` / `exclude` / `locations`: regexes. For example, `"locations": "India|Remote|Singapore|London|Hong Kong"` limits alerts to those places. Leave it empty for worldwide.
- `companies`: `{"name": "X", "boards": ["ashby:x"]}` (verified) or `{"name": "X", "slugs": ["x", "xai"]}` (auto-resolved with a name check).
- `workday`, `eightfold`, `sitemaps`, `render`, `feeds`, `markdown`, `simplify`: see the existing entries for the format.

## Optional channels (repo Settings → Secrets → Actions)
- `NTFY_TOPIC`: free phone push via the ntfy app.
- `SMTP_USER` + `SMTP_PASS` (+ `SMTP_TO`): send direct email through Gmail with an App Password.

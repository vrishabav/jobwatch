#!/usr/bin/env python3
"""jobwatch v3: poll many job sources for intern roles; alert exactly once per new role.

  python jobwatch/watch.py --mode api       fast sources (ATS APIs, feeds, sitemaps)        -> state + outbox
  python jobwatch/watch.py --mode render    JS-only career sites via headless Chromium      -> state + outbox
  python jobwatch/watch.py --notify         send everything in the outbox, then clear it
  add --dry-run to print instead of sending / saving

Exactly-once design (borrowed from zshah101's intern engine): collection only *queues* new roles in
outbox.json and marks them seen; the workflow commits + pushes that state; only then does --notify send
and clear the queue. A failed push means nothing was sent, so nothing is ever sent twice.

Every source/board gets a silent baseline on its first successful run (no flood of old roles);
after that only roles never seen before alert. Aggregator copies of a role already seen from a
direct board (or another aggregator) are suppressed. Dated items older than max_age_days never alert.
"""
import html as htmllib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urljoin, quote

DIR = os.environ.get("JOBWATCH_DIR") or os.path.dirname(os.path.abspath(__file__))
P = {n: os.path.join(DIR, f) for n, f in (("config", "config.json"), ("state", "state.json"), ("outbox", "outbox.json"),
                                            ("open", "open_roles.json"), ("open_md", "OPEN_ROLES.md"), ("coverage", "coverage.md"))}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
UA = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
DRY = "--dry-run" in sys.argv
MODE = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "api"
NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ helpers
def http(url, data=None, timeout=25, text=False):
    body = json.dumps(data).encode() if data is not None else None
    headers = dict(UA)
    if body:
        headers["Content-Type"] = "application/json"
    err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode("utf-8", "replace") if text else json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410, 422):
                raise
            err = e
        except Exception as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise err


def J(id_, company, title, url, loc="", extra="", pre=False, agg=False, via="", date=None):
    return {"id": id_, "company": (company or "").strip(), "title": re.sub(r"\s+", " ", title or "").strip(), "url": url,
            "loc": (loc or "").strip(), "extra": extra or "", "pre": pre, "agg": agg, "via": via, "date": date, "src": ""}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slug_title(url):
    segs = [s for s in url.split("?")[0].rstrip("/").split("/") if s]
    seg = segs[-1] if segs else url
    if seg.isdigit() and len(segs) > 1:
        seg = segs[-2]
    seg = re.sub(r"^\d+[-_]?|[-_]?\d+$", "", seg)
    return re.sub(r"[-_]+", " ", seg).strip().title() or url


def to_date(v):
    """Best-effort ISO date (YYYY-MM-DD) from epoch / ISO string / '3d' / 'Posted 3 Days Ago'."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            v = float(v)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).strftime("%Y-%m-%d")
        s = str(v).strip().lower()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        if "today" in s or s in ("0d", "new"):
            return TODAY
        if "yesterday" in s:
            return (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\s*\+?\s*(d|day|days)\b", s)
        if m and "+" not in s:
            return (NOW - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
        m = re.search(r"(\d+)\s*(mo|month)", s)
        if m:
            return (NOW - timedelta(days=30 * int(m.group(1)))).strftime("%Y-%m-%d")
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
            try:
                return datetime.strptime(str(v).strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ ATS adapters
def gh(slug, name, region=""):
    d = http(f"https://boards-api{region}.greenhouse.io/v1/boards/{slug}/jobs")
    return [J(f"gh{region}:{slug}:{j['id']}", name, j["title"], j["absolute_url"],
              (j.get("location") or {}).get("name", "")) for j in d.get("jobs", [])]


def gh_name(slug, region=""):
    try:
        return http(f"https://boards-api{region}.greenhouse.io/v1/boards/{slug}").get("name", "")
    except Exception:
        return ""


def lever(slug, name, eu=False):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    d = http(f"https://{host}/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        return []
    return [J(f"lv:{slug}:{j['id']}", name, j["text"], j["hostedUrl"], (j.get("categories") or {}).get("location", ""),
              (j.get("categories") or {}).get("commitment", ""), date=to_date(j.get("createdAt"))) for j in d]


def ashby(slug, name):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [J(f"ab:{slug}:{j['id']}", name, j["title"], j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j['id']}",
              j.get("location", ""), j.get("employmentType", ""), date=to_date(j.get("publishedAt")))
            for j in d.get("jobs", []) if j.get("isListed", True)]


def workable(slug, name):
    out, token = [], None
    for _ in range(6):
        body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            body["token"] = token
        d = http(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", body)
        for j in d.get("results", []):
            loc = j.get("location") or {}
            out.append(J(f"wk:{slug}:{j['shortcode']}", name, j["title"], f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
                         ", ".join(x for x in (loc.get("city"), loc.get("country")) if x), j.get("employment_type", ""),
                         date=to_date(j.get("published"))))
        token = d.get("nextPage")
        if not token:
            break
    return out


def smartrecruiters(slug, name):
    out, off = [], 0
    while off < 500:
        d = http(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={off}")
        cs = d.get("content", [])
        for j in cs:
            loc = j.get("location") or {}
            out.append(J(f"sr:{slug}:{j['id']}", name, j["name"], f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                         ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                         (j.get("typeOfEmployment") or {}).get("label", ""), date=to_date(j.get("releasedDate"))))
            out[-1]["_board"] = (j.get("company") or {}).get("name", "")
        off += 100
        if not cs or off >= d.get("totalFound", 0):
            break
    return out


def recruitee(slug, name):
    d = http(f"https://{slug}.recruitee.com/api/offers/")
    return [J(f"rc:{slug}:{j['id']}", name, j["title"], j.get("careers_url") or f"https://{slug}.recruitee.com/o/{j.get('slug')}",
              j.get("location", ""), date=to_date(j.get("published_at"))) for j in d.get("offers", [])]


def rippling(slug, name):
    d = http(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    return [J(f"rp:{slug}:{j.get('uuid')}", name, j.get("name"), j.get("url") or "",
              ((j.get("workLocation") or {}).get("label") or "")) for j in (d if isinstance(d, list) else [])]


ATS = {"greenhouse": lambda s, n: gh(s, n), "greenhouse_eu": lambda s, n: gh(s, n, ".eu"),
       "lever": lever, "lever_eu": lambda s, n: lever(s, n, True), "ashby": ashby, "workable": workable,
       "smartrecruiters": smartrecruiters, "recruitee": recruitee, "rippling": rippling}
AUTO_ORDER = ["greenhouse", "ashby", "lever", "greenhouse_eu", "smartrecruiters"]
NAME_CHECKED = {"greenhouse", "greenhouse_eu", "smartrecruiters"}


def name_ok(cfg_name, aliases, board_name):
    """Does a guessed board belong to this company? Whole-word match for short names, substring for long ones."""
    b, toks = norm(board_name), re.findall(r"[a-z0-9]+", (board_name or "").lower())
    first = norm(re.split(r"[\s/(&.-]+", cfg_name.strip())[0])
    full = {norm(cfg_name), norm(re.sub(r"\s*\(.*\)", "", cfg_name))} | {norm(a) for a in aliases or []}
    if any(len(c) >= 5 and c in b for c in full):
        return True
    return any(c and (c == b or c in toks) for c in full | ({first} if len(first) <= 4 else set()))


def fetch_company(c, cached):
    """Returns (name, [(board_key, jobs)], [failures])."""
    name = c["name"]
    if c.get("boards"):  # verified boards: poll all of them
        out, fails = [], []
        for b in c["boards"]:
            ats, slug = b.split(":", 1)
            try:
                out.append((f"{name}|{b}", ATS[ats](slug, name)))
            except Exception as e:
                fails.append(f"{b}: {str(e)[:80]}")
        return name, out, fails
    cands = [tuple(p) for p in cached]
    opts = ([c["ats"]] if isinstance(c.get("ats"), str) else c["ats"]) if c.get("ats") else AUTO_ORDER
    for slug in c.get("slugs", []):
        for ats in opts:
            if (ats, slug) not in cands:
                cands.append((ats, slug))
    note = ""
    for ats, slug in cands:
        try:
            js = ATS[ats](slug, name)
        except Exception:
            continue
        if not js:
            continue
        if not c.get("ats") and ats in NAME_CHECKED and [ats, slug] not in [list(p) for p in cached]:
            bn = gh_name(slug, ".eu" if ats.endswith("_eu") else "") if ats.startswith("greenhouse") else js[0].get("_board", "")
            if bn and not name_ok(name, c.get("aliases"), bn):
                note = f"slug '{slug}' on {ats} belongs to '{bn}', rejected"
                continue
        return name, [(f"{name}|{ats}:{slug}", js)], []
    return name, [], [note or "no board with jobs found (add the right slug under 'boards')"]


# ------------------------------------------------------------------ direct career-site adapters
def workday(w):
    out = {}
    for term in w.get("terms", ["intern", "co-op"]):
        off = 0
        while off < 200:
            d = http(f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}/jobs",
                     {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": term})
            ps = d.get("jobPostings", [])
            for p in ps:
                out[p["externalPath"]] = J(f"wd:{w['tenant']}:{p['externalPath']}", w["name"], p["title"],
                                           f"https://{w['host']}/en-US/{w['site']}{p['externalPath']}",
                                           p.get("locationsText", ""), date=to_date(p.get("postedOn")))
            off += 20
            if not ps or off >= d.get("total", 0):
                break
    return list(out.values())


def eightfold(e):
    base, dom, q = f"https://{e['host']}", e["domain"], e.get("query", "intern")
    out = {}
    try:  # classic endpoint first (what zshah101 uses), PCSX as fallback
        for start in range(0, 200, 10):
            d = http(f"{base}/api/apply/v2/jobs?domain={dom}&query={quote(q)}&start={start}&num=10&sort_by=timestamp")
            ps = d.get("positions") or []
            for p in ps:
                out[p["id"]] = J(f"ef:{e['name']}:{p['id']}", e["name"], p["name"],
                                 p.get("canonicalPositionUrl") or f"{base}/careers/job/{p['id']}", p.get("location", ""),
                                 date=to_date(p.get("t_create")))
            if len(ps) < 10:
                break
    except Exception:
        pass
    if not out:
        for start in range(0, 200, 10):
            d = http(f"{base}/api/pcsx/search?domain={dom}&query={quote(q)}&start={start}&sort_by=timestamp")
            ps = ((d.get("data") or d).get("positions")) or []
            for p in ps:
                out[p["id"]] = J(f"ef:{e['name']}:{p['id']}", e["name"], p["name"],
                                 urljoin(base, p.get("positionUrl") or f"/careers/job/{p['id']}"), "; ".join(p.get("locations") or []),
                                 date=to_date(p.get("postedTs")))
            if len(ps) < 10:
                break
    return list(out.values())


def amazon():
    out = []
    for off in range(0, 500, 100):
        d = http(f"https://www.amazon.jobs/en/search.json?base_query=intern&result_limit=100&offset={off}&sort=recent")
        js = d.get("jobs", [])
        for j in js:
            out.append(J(f"am:{j.get('id_icims') or j['id']}", "Amazon", j["title"], "https://www.amazon.jobs" + j["job_path"],
                         j.get("location", ""), j.get("business_category", ""), date=to_date(j.get("posted_date"))))
        if len(js) < 100:
            break
    return out


def html_links(h):
    page = http(h["url"], text=True)
    pat, out = re.compile(h["link_regex"]), {}
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S | re.I):
        href = urljoin(h["url"], htmllib.unescape(m.group(1))).split("#")[0]
        if not pat.search(href):
            continue
        txt = htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        out.setdefault(href.split("?")[0], (txt or slug_title(href), href))
    return [J(f"ht:{h['name']}:{k}", h["name"], t, u) for k, (t, u) in out.items()]


def sitemap(s):
    urls, queue, n = [], [s["url"]], 0
    pat = re.compile(s["url_regex"])
    while queue and n < 25:
        n += 1
        try:
            xml = http(queue.pop(0), text=True)
        except Exception:
            if n == 1:
                raise
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
        if "<sitemapindex" in xml:
            queue += locs
        else:
            urls += [htmllib.unescape(u) for u in locs if pat.search(u)]
    company = re.sub(r"\s*\((sitemap|rendered)\)", "", s["name"])
    return [J(f"sm:{s['name']}:{u}", company, slug_title(u), u) for u in dict.fromkeys(urls)]


def _pick(j, keys):
    for k in keys:
        v = j.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("label") or v.get("city")
        if isinstance(v, list):
            v = ", ".join(str(x.get("name", x) if isinstance(x, dict) else x) for x in v)
        if v not in (None, ""):
            return str(v)
    return ""


def json_feed(f):
    d = http(f["url"])
    if isinstance(d, dict):
        for k in ("jobs", "openings", "roles", "data", "results", "items", "positions"):
            if isinstance(d.get(k), list):
                d = d[k]
                break
    cat = re.compile(f["category_regex"], re.I) if f.get("category_regex") else None
    out = []
    for j in d if isinstance(d, list) else []:
        if not isinstance(j, dict) or j.get("active") is False or j.get("is_open") is False:
            continue
        url = _pick(j, f.get("url_keys") or ["url", "applyUrl", "apply_url", "job_url", "link", "absolute_url"])
        if url and not url.startswith("http"):
            url = "https://" + url.lstrip("/")
        title = _pick(j, f.get("title_keys") or ["title", "name", "position", "role", "jobTitle"])
        if not (title and url):
            continue
        if cat and not cat.search(_pick(j, ["category", "category_label", "roleCategory"]) or "x"):
            continue
        extra = " ".join(_pick(j, [k]) for k in (f.get("extra_keys") or [
            "category", "category_label", "job_type", "level", "seniority", "seniorityLevel", "type", "experience",
            "employment_type", "jobType", "program"]))
        out.append(J(f"{f['name']}:{j.get('id') or url}",
                     _pick(j, f.get("company_keys") or ["company", "company_name", "firm", "firmName", "firm_name", "employer"]) or f["name"],
                     title, url, _pick(j, ["location", "locations", "city", "office"]), extra, pre=bool(f.get("pre")), agg=True,
                     via=f["name"], date=to_date(_pick(j, f.get("date_keys") or ["date_posted", "posted_at", "datePosted", "date", "posted", "created_at"]))))
    return out


def markdown_table(m):
    """Rows like | Company | Position | Location | ... | <a href=apply> | Age |  (SpeedyApply, Simplify-style READMEs)."""
    text = http(m["url"], text=True)
    out, hdr = [], None
    for line in text.splitlines():
        if not line.startswith("|"):
            hdr = None if not line.strip() else hdr
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        plain = [htmllib.unescape(re.sub(r"<[^>]+>|\*\*|\[|\]\([^)]*\)", "", c)).strip() for c in cells]
        low = [p.lower() for p in plain]
        if any(h in low for h in ("company", "position", "role", "title")) and len(plain) >= 3:
            hdr = low
            continue
        if not hdr or set("".join(cells)) <= set("-: "):
            continue
        col = lambda *names: next((i for i, h in enumerate(hdr) if any(n in h for n in names)), None)
        ci, ti, li, ai = col("company"), col("position", "role", "title"), col("location"), col("age", "date", "posted")
        hrefs = [u for c in cells for u in re.findall(r'href="([^"]+)"|\]\((https?://[^)\s]+)\)', c) for u in u if u]
        apply = next((u for u in reversed(hrefs) if "imgur" not in u), None) or next(
            (c for c in plain if c.startswith("http")), None)
        if ci is None or ti is None or not apply or ci >= len(plain) or ti >= len(plain):
            continue
        comp = plain[ci].lstrip("↳ ").strip() or (out[-1]["company"] if out else "")
        if "🔒" in line or "closed" in plain[-1].lower():
            continue
        out.append(J(f"md:{m['name']}:{apply}", comp, plain[ti], apply, plain[li] if li is not None and li < len(plain) else "",
                     pre=bool(m.get("pre")), agg=True, via=m["name"],
                     date=to_date(plain[ai]) if ai is not None and ai < len(plain) else None))
    return out


def simplify(cfg):
    out, errs = [], []
    cat = re.compile(cfg.get("category_regex") or ".", re.I)
    for u in cfg["urls"]:
        try:
            for j in http(u):
                if not j.get("active") or j.get("is_visible") is False or not cat.search(j.get("category") or ""):
                    continue
                out.append(J(f"sf:{j['id']}", j["company_name"], j["title"], j["url"], ", ".join(j.get("locations") or []),
                             f"{j.get('category', '')} {' '.join(j.get('terms') or [])}", pre=True, agg=True, via="Simplify",
                             date=to_date(j.get("date_posted"))))
        except Exception as e:
            errs.append(f"{u}: {e}")
    if errs and not out:
        raise RuntimeError("; ".join(errs))
    return out


def render_all(items):
    res = {}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {r["name"]: ([], f"playwright not available: {e}") for r in items}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=BROWSER_UA, locale="en-US")
        for r in items:
            out, err = {}, None
            try:
                pat = re.compile(r["link_regex"])
                urls = [r["url"].replace("{page}", str(n)) for n in range(1, r.get("pages", 1) + 1)] if "{page}" in r["url"] else [r["url"]]
                for u in urls:
                    pg = ctx.new_page()
                    try:
                        pg.goto(u, wait_until="networkidle", timeout=60000)
                        for _ in range(r.get("scrolls", 3)):
                            pg.mouse.wheel(0, 5000)
                            pg.wait_for_timeout(700)
                        links = pg.eval_on_selector_all(
                            "a[href]", "els => els.map(e => [e.href, (e.innerText || e.getAttribute('aria-label') || '').trim()])")
                    finally:
                        pg.close()
                    for href, txt in links:
                        href = href.split("#")[0]
                        if pat.search(href):
                            t = re.sub(r"\s+", " ", (txt or "").split("\n")[0]).strip()
                            out.setdefault(href.split("?")[0], (t if len(t) > 3 else slug_title(href), href))
                if not out:
                    err = "page rendered but no job links matched (site may block bots or changed layout)"
            except Exception as e:
                err = str(e)[:160]
            company = re.sub(r"\s*\((sitemap|rendered)\)", "", r["name"])
            res[r["name"]] = ([J(f"rd:{r['name']}:{k}", company, t, u, pre=bool(r.get("pre"))) for k, (t, u) in out.items()], err)
        browser.close()
    return res


# ------------------------------------------------------------------ state / files
def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def load_state():
    st = {"seen": {}, "resolved": {}, "next_try": {}, "bootstrapped": [], "fails": {}, "warned": [], "coverage": {},
          "last_run": ""}
    loaded = load(P["state"], None)
    st.update(loaded or {})
    if loaded is None:
        st["version"] = 3
    if st.get("version", 2) < 3:  # migrate v2: company-level baselines -> board-level baselines
        boot = set(st["bootstrapped"])
        for name, pairs in list(st["resolved"].items()):
            if not pairs:
                continue
            pairs = [pairs] if isinstance(pairs[0], str) else pairs
            st["resolved"][name] = pairs
            if name in boot:
                boot |= {f"{name}|{a}:{s}" for a, s in pairs}
        if "Jane Street" in boot:
            boot.add("Jane Street|greenhouse:janestreet")  # v2 tracked Jane Street via its own site
        # aggregators are re-baselined silently once: v3 reads them with broader category/keyword rules
        boot -= {"Simplify feed", "QuantRoles", "Kadoa Quant"}
        st["bootstrapped"], st["version"] = sorted(boot), 3
    return st


def save_all(st, outbox, open_roles):
    if DRY:
        return
    for k, v in (("state", st), ("outbox", outbox), ("open", open_roles)):
        with open(P[k], "w") as f:
            json.dump(v, f, indent=0 if k != "state" else 1, sort_keys=(k == "state"))
    write_open_md(open_roles)
    rows = ["# Coverage (auto-generated)", "", "Last API run / render run results per source. Boards are `name|ats:slug`.", "",
            "| Source | Boards | Roles seen | Status |", "|---|---|---|---|"]
    for src in sorted(st["coverage"]):
        c = st["coverage"][src]
        rows.append(f"| {src} | {c['res']} | {c['n']} | {'OK' if c['status'] == 'OK' else 'NOT COVERED: ' + c['status']} |")
    with open(P["coverage"], "w") as f:
        f.write("\n".join(rows) + "\n")


def write_open_md(open_roles):
    by = {}
    for src, jobs in open_roles.items():
        for j in jobs:
            by.setdefault(j["company"], {})[j["title"].lower() + j["loc"].lower()] = j
    lines = [f"# Open intern roles ({sum(len(v) for v in by.values())}, updated {NOW:%Y-%m-%d %H:%M} UTC)", "",
             "Every currently open role matching your filters across all sources (duplicates merged). Alerts only fire for new ones.", ""]
    for comp in sorted(by, key=str.lower):
        lines.append(f"### {comp}")
        for j in sorted(by[comp].values(), key=lambda x: x["title"].lower()):
            extra = " · ".join(x for x in (j["loc"], j.get("via") and f"via {j['via']}", j.get("date")) if x)
            lines.append(f"- [{j['title']}]({j['url']})" + (f" — {extra}" if extra else ""))
        lines.append("")
    with open(P["open_md"], "w") as f:
        f.write("\n".join(lines))


# ------------------------------------------------------------------ alert delivery
def fmt_md(jobs):
    by = {}
    for j in jobs:
        by.setdefault(j["company"], []).append(j)
    out = []
    for comp in sorted(by, key=str.lower):
        out.append(f"### {comp}")
        for j in by[comp]:
            bits = [x for x in (j.get("loc"), j.get("via") and f"_via {j['via']}_") if x]
            out.append(f"- [{j['title']}]({j['url']})" + (f" — {' · '.join(bits)}" if bits else ""))
        out.append("")
    return "\n".join(out)


def fmt_text(jobs):
    return "\n".join(f"{j['company']}: {j['title']} ({j.get('loc', '')})\n  {j['url']}" for j in jobs)


def send_issue(title, body):
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        return False
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues",
                                 data=json.dumps({"title": title[:250], "body": f"@{owner}\n\n{body}"[:65000]}).encode(),
                                 headers={"User-Agent": "jobwatch", "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    urllib.request.urlopen(req, timeout=30).read()
    return True


def send_ntfy(title, text, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    h = {"User-Agent": "jobwatch", "Title": title.encode("ascii", "ignore").decode(), "Priority": "high", "Tags": "briefcase"}
    if click:
        h["Click"] = click
    urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=text[:3500].encode(), headers=h), timeout=30).read()
    return True


def send_smtp(title, text):
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if not (user and pw):
        return False
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = title, user, os.environ.get("SMTP_TO") or user
    m.set_content(text)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(m)
    return True


def deliver(title, jobs=None, note=""):
    jobs = jobs or []
    if DRY:
        print(f"\n=== {title} ===\n{fmt_text(jobs) if jobs else note}\n")
        return True
    ok = False
    push = (fmt_text(jobs[:6]) + (f"\n+{len(jobs) - 6} more" if len(jobs) > 6 else "")) if jobs else note
    for fn, args in ((send_issue, (title, fmt_md(jobs) if jobs else note)),
                     (send_ntfy, (title, push, jobs[0]["url"] if jobs else None)),
                     (send_smtp, (title, fmt_text(jobs) if jobs else note))):
        try:
            ok = fn(*args) or ok
        except Exception as e:
            print(f"[warn] {fn.__name__} failed: {e}", file=sys.stderr)
    return ok


def notify():
    ob = load(P["outbox"], {"jobs": [], "notes": []})
    jobs, notes = ob.get("jobs", []), ob.get("notes", [])
    if not jobs and not notes:
        print("outbox empty")
        return
    while jobs:
        batch = jobs[:100]
        comps = sorted({j["company"] for j in batch}, key=str.lower)
        title = f"{len(batch)} new intern role(s): " + ", ".join(comps[:5]) + (" …" if len(comps) > 5 else "")
        if not deliver(title, batch):
            break
        jobs = jobs[100:]
    if notes and deliver(f"jobwatch: {len(notes)} source(s) need attention", None,
                         "These sources errored or returned nothing. See coverage.md; fix them in config.json "
                         "(or ignore if the firm simply has no open roles).\n\n" + "\n".join(notes)):
        notes = []
    if not DRY:
        with open(P["outbox"], "w") as f:
            json.dump({"jobs": jobs, "notes": notes}, f, indent=0)
    if jobs:
        print("Delivery failed; remaining roles stay queued for the next run.", file=sys.stderr)
        sys.exit(1)


# ------------------------------------------------------------------ collect
def collect(cfg, st, mode):
    S = {}  # board/source key -> dict(jobs, err, agg, group, res)

    def put(key, jobs, err=None, agg=False, group=None, res="-"):
        for j in jobs:
            j["src"] = key
        S[key] = {"jobs": jobs, "err": err, "agg": agg, "group": group or key, "res": res}

    def run(label, fn, **meta):
        try:
            jobs = fn()
            return label, jobs, (None if jobs else "returned 0 jobs"), meta
        except Exception as e:
            return label, [], str(e)[:160], meta

    if mode in ("api", "all"):
        with ThreadPoolExecutor(10) as pool:
            comp_f, other_f = [], []
            for c in cfg["companies"]:
                nm = c["name"]
                cached = st["resolved"].get(nm) or []
                if not c.get("boards") and not cached and st["next_try"].get(nm, "") > TODAY:
                    put(f"{nm}|!", [], st["coverage"].get(f"{nm}|!", {}).get("status", "unresolved (retried daily)"), group=nm)
                    continue
                comp_f.append(pool.submit(fetch_company, c, cached))
            if cfg.get("simplify"):
                other_f.append(pool.submit(run, "Simplify feed", lambda: simplify(cfg["simplify"]), agg=True))
            for f in cfg.get("feeds", []):
                other_f.append(pool.submit(run, f["name"], lambda f=f: json_feed(f), agg=True))
            for m in cfg.get("markdown", []):
                other_f.append(pool.submit(run, m["name"], lambda m=m: markdown_table(m), agg=True))
            if cfg.get("amazon"):
                other_f.append(pool.submit(run, "Amazon", amazon))
            for w in cfg.get("workday", []):
                other_f.append(pool.submit(run, w["name"], lambda w=w: workday(w)))
            for e in cfg.get("eightfold", []):
                other_f.append(pool.submit(run, e["name"], lambda e=e: eightfold(e)))
            for h in cfg.get("html", []):
                other_f.append(pool.submit(run, h["name"], lambda h=h: html_links(h), pre=h.get("pre")))
            for s in cfg.get("sitemaps", []):
                other_f.append(pool.submit(run, s["name"], lambda s=s: sitemap(s)))
            for t in comp_f:
                name, boards, fails = t.result()
                if boards:
                    st["resolved"][name] = [k.split("|", 1)[1].split(":", 1) for k, _ in boards]
                    st["next_try"].pop(name, None)
                else:
                    st["next_try"][name] = (NOW + timedelta(days=1)).strftime("%Y-%m-%d")
                for key, jobs in boards:
                    put(key, jobs, None if jobs else "board returned 0 jobs", group=name, res=key.split("|", 1)[1])
                if fails:
                    put(f"{name}|!", [], "; ".join(fails), group=name)
            for t in other_f:
                label, jobs, err, meta = t.result()
                if meta.get("pre"):
                    for j in jobs:
                        j["pre"] = True
                put(label, jobs, err, agg=bool(meta.get("agg")))
    if mode in ("render", "all"):
        for name, (jobs, err) in render_all(cfg.get("render", [])).items():
            put(name, jobs, err)
    return S


def dkey(j):
    t = re.sub(r"\([^)]*\)|\[[^]]*\]", "", j["title"].lower())
    t = re.sub(r"\b(summer|winter|spring|fall|autumn)?\s*'?20\d\d(\s*[-/]\s*(20)?\d\d)?\b", "", t)
    return norm(j["company"])[:5] + "|" + norm(t)


def main():
    if "--notify" in sys.argv:
        return notify()
    cfg, st = load(P["config"], None), load_state()
    kw = re.compile(cfg["keywords"], re.I)
    ex = re.compile(cfg["exclude"], re.I) if cfg.get("exclude") else None
    loc = re.compile(cfg["locations"], re.I) if cfg.get("locations") else None
    max_age = cfg.get("max_age_days", 21)
    age_cut = (NOW - timedelta(days=max_age)).strftime("%Y-%m-%d") if max_age else ""
    silent_first = cfg.get("first_run", "silent") == "silent"
    S = collect(cfg, st, MODE)
    outbox = load(P["outbox"], {"jobs": [], "notes": []})
    open_roles = load(P["open"], {})
    seen, new_jobs, run_dk = st["seen"], [], set()
    boot = set(st["bootstrapped"])

    for key, s in sorted(S.items(), key=lambda kv: kv[1]["agg"]):  # direct boards first, aggregators last
        if s["err"] and not s["jobs"]:
            continue
        first, matched = key not in boot, []
        for j in s["jobs"]:
            if not j["pre"] and not kw.search(f"{j['title']} {j['extra']}"):
                continue
            if (ex and ex.search(j["title"])) or (loc and j["loc"] and not loc.search(j["loc"])):
                continue
            dk = dkey(j)
            if s["agg"] and (dk in run_dk or ("dk:" + dk in seen and j["id"] not in seen)):
                seen.setdefault(j["id"], TODAY)   # same role already known from another source
                continue
            matched.append({k: j[k] for k in ("company", "title", "url", "loc", "via", "date")})
            run_dk.add(dk)
            if j["id"] in seen:
                continue
            seen[j["id"]] = TODAY
            seen.setdefault("dk:" + dk, TODAY)
            if (first and silent_first) or (j["date"] and age_cut and j["date"] < age_cut):
                continue  # baseline or stale posting: remember silently
            new_jobs.append({k: j[k] for k in ("company", "title", "url", "loc", "via")})
        if not s["err"]:
            open_roles[key] = matched
            boot.add(key)

    # health: warn once per broken source (on first failure if it never worked, else after 3 consecutive failures)
    for key, s in S.items():
        g = s["group"]
        if not s["err"]:
            continue
        st["fails"][key] = st["fails"].get(key, 0) + 1
        never_ok = not any(b == key or b.startswith(g + "|") for b in boot) and key not in boot
        if (never_ok or st["fails"][key] >= 3) and key not in st["warned"]:
            outbox["notes"].append(f"- **{key}**: {s['err']}")
            st["warned"].append(key)
    for key, s in S.items():
        if not s["err"] and key in st["warned"]:
            st["warned"].remove(key)
            st["fails"].pop(key, None)

    outbox["jobs"] += new_jobs
    st["bootstrapped"] = sorted(boot)
    for key, s in S.items():
        st["coverage"][key] = {"res": s["res"], "n": len(s["jobs"]), "status": "OK" if not s["err"] else s["err"]}
    live = {j["id"] for s in S.values() for j in s["jobs"]}
    cutoff = (NOW - timedelta(days=365)).strftime("%Y-%m-%d")
    st["seen"] = {k: v for k, v in seen.items() if k in live or v >= cutoff}
    st["last_run"] = TODAY
    if MODE in ("api", "all"):  # drop open-role lists of sources no longer configured/polled by this mode
        keep = set(S) | {r["name"] for r in cfg.get("render", [])}
        open_roles = {k: v for k, v in open_roles.items() if k in keep}
    save_all(st, outbox, open_roles)
    ok_n = sum(1 for s in S.values() if not s["err"])
    print(f"[{MODE}] {ok_n}/{len(S)} sources OK, {len(new_jobs)} new roles queued, "
          f"{sum(len(v) for v in open_roles.values())} open roles tracked")
    if DRY:
        for j in new_jobs[:50]:
            print("NEW", j["company"], "|", j["title"], "|", j["url"])


if __name__ == "__main__":
    main()

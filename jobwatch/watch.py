#!/usr/bin/env python3
"""jobwatch v2: poll many job sources for intern roles and alert on new ones.

Modes:   --mode api     (default) fast JSON/HTML/sitemap sources, no browser needed
         --mode render  JS-heavy career sites rendered with Playwright (Google, Meta, Apple, Citadel...)
         --mode all     both
         --dry-run      print instead of sending, do not save state

Source types (all configured in config.json):
  companies   Greenhouse (US+EU) / Lever / Ashby / Workable / SmartRecruiters / Recruitee, auto-resolved by slug guess
  workday, eightfold, amazon, janestreet, html, sitemaps, render   direct career sites
  simplify, feeds   third-party aggregators/datasets (Simplify, QuantRoles, Kadoa Quant ...)
Alerts: GitHub Issue (emails you), optional ntfy.sh push, optional SMTP.  Stdlib only (+ playwright for render).
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
CONFIG, STATE, COVERAGE = (os.path.join(DIR, n) for n in ("config.json", "state.json", "coverage.md"))
UA = {"User-Agent": "Mozilla/5.0 (compatible; jobwatch/2.0; personal job alerts)"}
DRY = "--dry-run" in sys.argv
MODE = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "api"


# ------------------------------------------------------------------ http
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
            if e.code in (400, 401, 403, 404, 410):
                raise
            err = e
        except Exception as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise err


def J(id_, company, title, url, loc="", extra="", pre=False, agg=False, via=""):
    return {"id": id_, "company": (company or "").strip(), "title": (title or "").strip(), "url": url,
            "loc": (loc or "").strip(), "extra": extra or "", "pre": pre, "agg": agg, "via": via}


def slug_title(url):
    seg = [s for s in url.split("?")[0].rstrip("/").split("/") if s][-1]
    seg = re.sub(r"^\d+[-_]?|[-_]?\d+$", "", seg)
    return re.sub(r"[-_]+", " ", seg).strip().title() or url


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ------------------------------------------------------------------ ATS adapters (company boards)
def gh(slug, name, region=""):
    d = http(f"https://boards-api{region}.greenhouse.io/v1/boards/{slug}/jobs")
    return [J(f"gh{region}:{slug}:{j['id']}", name, j["title"], j["absolute_url"],
              (j.get("location") or {}).get("name", "")) for j in d.get("jobs", [])]


def gh_name(slug, region=""):
    try:
        return http(f"https://boards-api{region}.greenhouse.io/v1/boards/{slug}").get("name", "")
    except Exception:
        return ""


def lever(slug, name):
    d = http(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        return []
    return [J(f"lv:{slug}:{j['id']}", name, j["text"], j["hostedUrl"],
              (j.get("categories") or {}).get("location", ""),
              (j.get("categories") or {}).get("commitment", "")) for j in d]


def ashby(slug, name):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [J(f"ab:{slug}:{j['id']}", name, j["title"],
              j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j['id']}",
              j.get("location", ""), j.get("employmentType", ""))
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
            out.append(J(f"wk:{slug}:{j['shortcode']}", name, j["title"],
                         f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
                         ", ".join(x for x in (loc.get("city"), loc.get("country")) if x), j.get("employment_type", "")))
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
                         (j.get("typeOfEmployment") or {}).get("label", "")))
        off += 100
        if not cs or off >= d.get("totalFound", 0):
            break
    return out


def recruitee(slug, name):
    d = http(f"https://{slug}.recruitee.com/api/offers/")
    return [J(f"rc:{slug}:{j['id']}", name, j["title"], j.get("careers_url") or f"https://{slug}.recruitee.com/o/{j.get('slug')}",
              j.get("location", "")) for j in d.get("offers", [])]


ATS = {"greenhouse": lambda s, n: gh(s, n), "greenhouse_eu": lambda s, n: gh(s, n, ".eu"),
       "lever": lever, "ashby": ashby, "workable": workable,
       "smartrecruiters": smartrecruiters, "recruitee": recruitee}
AUTO_ORDER = ["greenhouse", "greenhouse_eu", "ashby", "lever"]


def name_ok(cfg_name, aliases, board_name):
    """Does a guessed board belong to this company? Whole-word match on short names, substring only for long ones."""
    b, toks = norm(board_name), re.findall(r"[a-z0-9]+", board_name.lower())
    first = norm(re.split(r"[\s/(&-]+", cfg_name.strip())[0])
    full = {norm(cfg_name), norm(cfg_name.replace(".", ""))} | {norm(a) for a in aliases or []}
    if any(len(c) >= 5 and c in b for c in full):
        return True
    return any(c and (c == b or c in toks) for c in full | ({first} if len(first) <= 4 else set()))


def fetch_company(c, cached):
    name, pinned = c["name"], bool(c.get("ats"))
    cands = [tuple(p) for p in cached]
    ats_opts = ([c["ats"]] if isinstance(c.get("ats"), str) else c["ats"]) if pinned else AUTO_ORDER
    for slug in c["slugs"]:
        for ats in ats_opts:
            if (ats, slug) not in cands:
                cands.append((ats, slug))
    good, jobs, note = [], [], ""
    for ats, slug in cands:
        try:
            js = ATS[ats](slug, name)
        except Exception:
            continue
        if not js:
            continue
        if not pinned and ats.startswith("greenhouse") and [ats, slug] not in [list(p) for p in cached]:
            bn = gh_name(slug, ".eu" if ats.endswith("_eu") else "")
            if bn and not name_ok(name, c.get("aliases"), bn):
                note = f"slug '{slug}' on {ats} belongs to '{bn}', rejected"
                continue
        good.append((ats, slug))
        jobs += js
        if not c.get("all"):
            break
    return name, good, jobs, (None if good else (note or "no board with jobs found"))


# ------------------------------------------------------------------ direct career-site adapters
def workday(w):
    out, off = [], 0
    while off < 200:
        d = http(f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}/jobs",
                 {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": w.get("query", "intern")})
        ps = d.get("jobPostings", [])
        if not ps:
            break
        for p in ps:
            out.append(J(f"wd:{w['tenant']}:{p['externalPath']}", w["name"], p["title"],
                         f"https://{w['host']}/en-US/{w['site']}{p['externalPath']}", p.get("locationsText", "")))
        off += 20
        if off >= d.get("total", 0):
            break
    return out


def eightfold(e):
    base, dom, q = f"https://{e['host']}", e["domain"], e.get("query", "intern")
    out = {}
    try:  # newer PCSX endpoint
        for start in range(0, 100, 10):
            d = http(f"{base}/api/pcsx/search?domain={dom}&query={quote(q)}&start={start}&sort_by=timestamp")
            ps = ((d.get("data") or d).get("positions")) or []
            if not ps:
                break
            for p in ps:
                u = p.get("positionUrl") or f"/careers/job/{p['id']}"
                out[p["id"]] = J(f"ef:{e['name']}:{p['id']}", e["name"], p["name"], urljoin(base, u),
                                 "; ".join(p.get("locations") or []))
    except Exception:
        pass
    if not out:  # classic apply API
        for start in range(0, 100, 10):
            d = http(f"{base}/api/apply/v2/jobs?domain={dom}&query={quote(q)}&start={start}&num=10")
            ps = d.get("positions") or []
            if not ps:
                break
            for p in ps:
                out[p["id"]] = J(f"ef:{e['name']}:{p['id']}", e["name"], p["name"],
                                 p.get("canonicalPositionUrl") or f"{base}/careers/job/{p['id']}", p.get("location", ""))
    return list(out.values())


def amazon():
    out = []
    for off in range(0, 400, 100):
        d = http(f"https://www.amazon.jobs/en/search.json?base_query=intern&result_limit=100&offset={off}&sort=recent")
        js = d.get("jobs", [])
        if not js:
            break
        for j in js:
            out.append(J(f"am:{j.get('id_icims') or j['id']}", "Amazon", j["title"],
                         "https://www.amazon.jobs" + j["job_path"], j.get("location", ""), j.get("business_category", "")))
    return out


def janestreet():
    d = http("https://www.janestreet.com/jobs/main.json")
    if isinstance(d, dict):
        d = d.get("jobs") or d.get("positions") or []
    return [J(f"js:{j['id']}", "Jane Street", j["position"],
              f"https://www.janestreet.com/join-jane-street/position/{j['id']}/",
              j.get("city", ""), f"{j.get('availability', '')} {j.get('duration', '')}") for j in d]


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
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
        if "<sitemapindex" in xml:
            queue += locs
        else:
            urls += [u for u in locs if pat.search(u)]
    return [J(f"sm:{s['name']}:{u}", s["name"], slug_title(u), u) for u in dict.fromkeys(urls)]


def json_feed(f):
    d = http(f["url"])
    if isinstance(d, dict):
        for k in ("jobs", "openings", "roles", "data", "results", "items", "positions"):
            if isinstance(d.get(k), list):
                d = d[k]
                break
    out = []

    def pick(j, keys):
        for k in keys:
            v = j.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("label")
            if isinstance(v, list):
                v = ", ".join(str(x.get("name", x) if isinstance(x, dict) else x) for x in v)
            if v:
                return str(v)
        return ""
    for j in d if isinstance(d, list) else []:
        if not isinstance(j, dict):
            continue
        url = pick(j, f.get("url_keys") or ["url", "apply_url", "job_url", "link", "applyUrl", "absolute_url"])
        if url and not url.startswith("http"):
            url = "https://" + url.lstrip("/")
        title = pick(j, f.get("title_keys") or ["title", "name", "position", "role"])
        if not (title and url):
            continue
        extra = " ".join(pick(j, [k]) for k in (f.get("extra_keys") or [
            "category", "category_label", "job_type", "level", "seniority", "type", "experience",
            "experience_level", "employment_type", "tags"]))
        out.append(J(f"{f['name']}:{j.get('id') or url}", pick(j, ["firm", "company", "company_name", "firm_name", "employer"]) or f["name"],
                     title, url, pick(j, ["location", "locations", "city", "office"]), extra, agg=True, via=f["name"]))
    return out


def simplify(cfg):
    out, errs = [], []
    cats = set(cfg.get("categories") or [])
    for u in cfg["urls"]:
        try:
            for j in http(u):
                if not j.get("active") or j.get("is_visible") is False:
                    continue
                if cats and j.get("category") not in cats:
                    continue
                out.append(J(f"sf:{j['id']}", j["company_name"], j["title"], j["url"], ", ".join(j.get("locations") or []),
                             f"{j.get('category', '')} {' '.join(j.get('terms') or [])}", pre=True, agg=True, via="Simplify"))
        except Exception as e:
            errs.append(f"{u}: {e}")
    if errs and not out:
        raise RuntimeError("; ".join(errs))
    return out


def render_all(items):
    """Render JS-heavy career pages in one headless browser. Returns {name: (jobs, err)}."""
    res = {}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {r["name"]: ([], f"playwright not available: {e}") for r in items}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA["User-Agent"].replace("jobwatch/2.0; personal job alerts", "Chrome/124 Safari/537.36"))
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
                            out.setdefault(href.split("?")[0], (re.sub(r"\s+", " ", txt).split("\n")[0].strip() or slug_title(href), href))
                if not out:
                    err = "page rendered but no job links matched"
            except Exception as e:
                err = str(e)[:160]
            res[r["name"]] = ([J(f"rd:{r['name']}:{k}", r["name"], t, u, pre=bool(r.get("pre")))
                               for k, (t, u) in out.items()], err)
        browser.close()
    return res


# ------------------------------------------------------------------ state
def load_state():
    base = {"seen": {}, "resolved": {}, "next_try": {}, "bootstrapped": [], "fails": {}, "warned": [],
            "coverage": {}, "last_run": ""}
    if os.path.exists(STATE):
        base.update(json.load(open(STATE)))
    return base


def as_pairs(v):
    if not v:
        return []
    return [list(v)] if isinstance(v[0], str) else [list(p) for p in v]


# ------------------------------------------------------------------ alert delivery
def fmt_md(jobs):
    by = {}
    for j in jobs:
        by.setdefault(j["company"], []).append(j)
    lines = []
    for comp in sorted(by):
        lines.append(f"### {comp}")
        for j in by[comp]:
            loc = f" - {j['loc']}" if j["loc"] else ""
            via = f" _(via {j['via']})_" if j["via"] else ""
            lines.append(f"- [{j['title']}]({j['url']}){loc}{via}")
        lines.append("")
    return "\n".join(lines)


def fmt_text(jobs):
    return "\n".join(f"{j['company']}: {j['title']} ({j['loc']})\n  {j['url']}" for j in jobs)


def send_issue(title, body):
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        return False
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title[:250], "body": f"@{owner}\n\n{body}"[:65000]}).encode(),
        headers={**UA, "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    urllib.request.urlopen(req, timeout=30).read()
    return True


def send_ntfy(title, text, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    h = {**UA, "Title": title.encode("ascii", "ignore").decode(), "Priority": "high", "Tags": "briefcase"}
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


def notify(title, jobs=None, note=""):
    jobs = jobs or []
    if DRY:
        print(f"\n=== {title} ===\n{fmt_text(jobs) if jobs else note}\n")
        return True
    ok = False
    chunks = [jobs[i:i + 120] for i in range(0, len(jobs), 120)] or [[]]
    for n, ch in enumerate(chunks, 1):
        t = title if len(chunks) == 1 else f"{title} ({n}/{len(chunks)})"
        push = (fmt_text(ch[:6]) + (f"\n+{len(ch) - 6} more" if len(ch) > 6 else "")) if ch else note
        for fn, args in ((send_issue, (t, fmt_md(ch) if ch else note)),
                         (send_ntfy, (t, push, ch[0]["url"] if ch else None)),
                         (send_smtp, (t, fmt_text(ch) if ch else note))):
            try:
                ok = fn(*args) or ok
            except Exception as e:
                print(f"[warn] {fn.__name__} failed: {e}", file=sys.stderr)
    return ok


# ------------------------------------------------------------------ collect sources
def collect(cfg, st, mode):
    S = {}  # name -> dict(jobs, err, res, agg, silent)

    def put(name, jobs, err=None, res="", agg=False, silent=False):
        S[name] = {"jobs": jobs, "err": err, "res": res, "agg": agg, "silent": silent}

    def run(label, fn, **meta):
        try:
            jobs = fn()
            return label, jobs, (None if jobs else "returned 0 jobs"), meta
        except Exception as e:
            return label, [], str(e)[:160], meta

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if mode in ("api", "all"):
        with ThreadPoolExecutor(8) as pool:
            comp_f, other_f = [], []
            for c in cfg["companies"]:
                nm = c["name"]
                cached = as_pairs(st["resolved"].get(nm))
                if not cached and st["next_try"].get(nm, "") > today:
                    put(nm, [], st["coverage"].get(nm, {}).get("status", "unresolved (retrying daily)"))
                    continue
                comp_f.append(pool.submit(fetch_company, c, cached))
            if cfg.get("simplify"):
                other_f.append(pool.submit(run, "Simplify feed", lambda: simplify(cfg["simplify"]), agg=True, silent=True))
            for f in cfg.get("feeds", []):
                other_f.append(pool.submit(run, f["name"], lambda f=f: json_feed(f), agg=True, silent=f.get("silent_first_run", True)))
            if cfg.get("amazon"):
                other_f.append(pool.submit(run, "Amazon", amazon, silent=True))
            if cfg.get("janestreet"):
                other_f.append(pool.submit(run, "Jane Street", janestreet))
            for w in cfg.get("workday", []):
                other_f.append(pool.submit(run, w["name"], lambda w=w: workday(w)))
            for e in cfg.get("eightfold", []):
                other_f.append(pool.submit(run, e["name"], lambda e=e: eightfold(e)))
            for h in cfg.get("html", []):
                other_f.append(pool.submit(run, h["name"], lambda h=h: html_links(h), pre=h.get("pre")))
            for s in cfg.get("sitemaps", []):
                other_f.append(pool.submit(run, s["name"], lambda s=s: sitemap(s)))
            for t in comp_f:
                name, good, jobs, err = t.result()
                if good:
                    st["resolved"][name] = [list(p) for p in good]
                else:
                    st["next_try"][name] = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
                put(name, jobs, err, res=", ".join("/".join(p) for p in good))
            for t in other_f:
                label, jobs, err, meta = t.result()
                if meta.get("pre"):
                    for j in jobs:
                        j["pre"] = True
                put(label, jobs, err, agg=bool(meta.get("agg")), silent=bool(meta.get("silent")))
    if mode in ("render", "all"):
        items = cfg.get("render", [])
        for name, (jobs, err) in render_all(items).items():
            put(name, jobs, err)
    return S


def dkey(j):
    t = re.sub(r"\([^)]*\)", "", j["title"].lower())
    t = re.sub(r"\b(summer|winter|spring|fall|autumn)?\s*20\d\d\b|[-–]\s*20\d\d(-\d\d)?", "", t)
    return norm(j["company"])[:5] + "|" + norm(t)


def main():
    cfg, st = json.load(open(CONFIG)), load_state()
    kw = re.compile(cfg["keywords"], re.I)
    ex = re.compile(cfg["exclude"], re.I) if cfg.get("exclude") else None
    loc = re.compile(cfg["locations"], re.I) if cfg.get("locations") else None
    S = collect(cfg, st, MODE)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seen, new_jobs, snapshot = st["seen"], [], []
    run_dk = set()
    for src, s in sorted(S.items(), key=lambda kv: kv[1]["agg"]):  # direct sources first, aggregators last
        if s["err"]:
            continue
        first = src not in st["bootstrapped"]
        for j in s["jobs"]:
            if not j["pre"] and not kw.search(f"{j['title']} {j['extra']}"):
                continue
            if (ex and ex.search(j["title"])) or (loc and j["loc"] and not loc.search(j["loc"])):
                continue
            dk = dkey(j)
            if not s["agg"]:
                run_dk.add(dk)
                seen.setdefault("dk:" + dk, today)
            if j["id"] in seen:
                continue
            if s["agg"] and (dk in run_dk or "dk:" + dk in seen):  # same role already covered by a direct source
                seen[j["id"]] = today
                continue
            if first and s["silent"]:
                seen[j["id"]] = today
                continue
            (snapshot if first else new_jobs).append(j)

    warn = []
    for src, s in S.items():
        if not s["err"]:
            st["fails"].pop(src, None)
            if src in st["warned"]:
                st["warned"].remove(src)
            continue
        st["fails"][src] = st["fails"].get(src, 0) + 1
        never_ok = src not in st["bootstrapped"]
        if (never_ok or st["fails"][src] >= 3) and src not in st["warned"]:
            warn.append(f"- **{src}**: {s['err']}")
            st["warned"].append(src)

    delivered = True
    if new_jobs:
        delivered = notify(f"{len(new_jobs)} new intern role(s): " + ", ".join(sorted({j['company'] for j in new_jobs})[:5]), new_jobs)
    if snapshot and delivered:
        delivered = notify(f"Initial snapshot: {len(snapshot)} open intern roles", snapshot)
    if warn:
        notify(f"jobwatch coverage: {len(warn)} source(s) not reachable", None,
               "These sources returned nothing or errored. Fix the slug/ATS/URL in config.json or ignore if the firm has no open roles.\n\n" + "\n".join(warn))
    if not delivered:
        print("No alert channel succeeded; leaving state untouched so we retry next run.", file=sys.stderr)
        sys.exit(1)

    for j in new_jobs + snapshot:
        seen[j["id"]] = today
    for src, s in S.items():
        if not s["err"] and src not in st["bootstrapped"]:
            st["bootstrapped"].append(src)
        st["coverage"][src] = {"res": s["res"] or "-", "n": len(s["jobs"]), "status": "OK" if not s["err"] else s["err"], "mode": MODE}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=150)).strftime("%Y-%m-%d")
    live = {j["id"] for s in S.values() for j in s["jobs"]}
    st["seen"] = {k: v for k, v in seen.items() if k in live or v >= cutoff}
    st["last_run"] = today

    rows = ["# Coverage (auto-generated)", "", "| Source | Resolved to | Roles seen | Status |", "|---|---|---|---|"]
    for src in sorted(st["coverage"]):
        c = st["coverage"][src]
        rows.append(f"| {src} | {c['res']} | {c['n']} | {'OK' if c['status'] == 'OK' else 'NOT COVERED: ' + c['status']} |")
    if not DRY:
        open(COVERAGE, "w").write("\n".join(rows) + "\n")
        json.dump(st, open(STATE, "w"), indent=1, sort_keys=True)
    ok_n = sum(1 for s in S.values() if not s["err"])
    print(f"[{MODE}] {ok_n}/{len(S)} sources OK, {len(new_jobs)} new, {len(snapshot)} snapshot")


if __name__ == "__main__":
    main()

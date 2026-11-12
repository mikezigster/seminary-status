#!/usr/bin/env python3
"""Build a read-only seminary status page from Todoist.

Reads every class project nested under the parent project named in
config.json (e.g. "MAC Classes"), sorts open tasks into Overdue / Due this
week / The plan ahead, and writes a single self-contained index.html.

Usage:
    TODOIST_TOKEN=... python build.py --out site
    python build.py --fixture sample.json --today 2026-11-11 --out site   (offline test)

Standard library only, so it runs on a stock GitHub Actions runner.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://api.todoist.com/api/v1"
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- fetching

def api_get(path, token, **params):
    """GET a paginated Todoist API v1 list and return every result."""
    results, cursor = [], None
    while True:
        query = {**params, "limit": 200}
        if cursor:
            query["cursor"] = cursor
        url = f"{API}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        if isinstance(body, list):  # defensive: unpaginated shape
            return body
        results.extend(body.get("results", []))
        cursor = body.get("next_cursor")
        if not cursor:
            return results


def fetch_from_todoist(token, parent_name):
    projects = api_get("/projects", token)
    parent = next((p for p in projects if p["name"].strip().lower() == parent_name.strip().lower()), None)
    if parent is None:
        sys.exit(f"Could not find a Todoist project named '{parent_name}'. Check config.json.")
    classes = [p for p in projects if p.get("parent_id") == parent["id"] and not p.get("is_archived")]
    data = {"parent": parent, "classes": classes, "tasks": {}, "sections": {}}
    for p in [parent, *classes]:
        data["tasks"][p["id"]] = api_get("/tasks", token, project_id=p["id"])
        data["sections"][p["id"]] = api_get("/sections", token, project_id=p["id"])
    return data


# ---------------------------------------------------------------- helpers

def due_date(task):
    due = task.get("due") or {}
    raw = due.get("date") if isinstance(due, dict) else None
    return dt.date.fromisoformat(raw[:10]) if raw else None


def deadline_date(task):
    d = task.get("deadline") or {}
    raw = d.get("date") if isinstance(d, dict) else None
    return dt.date.fromisoformat(raw[:10]) if raw else None


def class_code(project_name):
    m = re.search(r"\(([A-Z]{2,4}\s?\d{2,4}[A-Z]?)\)\s*$", project_name)
    return m.group(1) if m else project_name


def class_title(project_name):
    return re.sub(r"\s*\([^)]*\)\s*$", "", project_name).strip()


def fmt_day(d, today):
    if d == today:
        return "Today"
    return f"{d:%a}, {d:%b} {d.day}"


def fmt_long(d):
    return f"{d:%A}, {d:%B} {d.day}"


def fmt_minutes(total):
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} hr {m} min"
    return f"{h} hr" if h else f"{m} min"


def tidy_title(content):
    """'Watch: Lecture 16 - Seeing Christ (47min)' -> 'Watch Lecture 16 — Seeing Christ'."""
    t = re.sub(r"\s*\(\s*\d+\s*min\s*\)\s*$", "", content.strip(), flags=re.I)
    t = re.sub(r"^(Watch|Read|Listen|Write):\s*", r"\1 ", t)
    t = t.replace(" - ", " — ")
    t = re.sub(r"(\d)-(\d)", "\\1\u2013\\2", t)  # chs 7-10 -> chs 7–10
    return t


def kind_of(content):
    c = content.lower()
    if c.startswith("watch"):
        return "Lecture"
    if c.startswith("read"):
        return "Reading"
    if c.startswith("listen"):
        return "Listening"
    if "quiz" in c:
        return "Quiz"
    if "paper" in c and not any(w in c for w in ("outline", "draft", "revise", "polish")):
        return "Paper"
    if "meeting" in c:
        return "Meeting"
    return ""


def lecture_minutes(content):
    m = re.search(r"\(\s*(\d+)\s*min\s*\)", content, flags=re.I)
    return int(m.group(1)) if m else 0


def duration_text(task):
    d = task.get("duration") or {}
    if not isinstance(d, dict) or not d.get("amount"):
        return ""
    if d.get("unit") == "day":
        return f"{d['amount']} day" + ("s" if d["amount"] != 1 else "")
    return fmt_minutes(int(d["amount"]))


def current_week(sections, today):
    """Find 'Week 9 (11/9-15): Prophets & Daniel' covering today -> (9, 10, 'Prophets & Daniel')."""
    pat = re.compile(r"Week\s+(\d+)\s*\((\d{1,2})/(\d{1,2})\s*-\s*(?:(\d{1,2})/)?(\d{1,2})\)\s*:?\s*(.*)", re.I)
    weeks, hit = [], None
    for s in sections:
        m = pat.match(s.get("name", ""))
        if not m:
            continue
        n, m1, d1, m2, d2, topic = m.groups()
        weeks.append(int(n))
        try:
            start = dt.date(today.year, int(m1), int(d1))
            end = dt.date(today.year, int(m2 or m1), int(d2))
        except ValueError:
            continue
        if start <= today <= end:
            hit = (int(n), topic.strip())
    if hit:
        return hit[0], max(weeks), hit[1]
    return None


# ---------------------------------------------------------------- model

def build_model(data, today, cfg):
    classes = data["classes"]
    multi = len([c for c in classes if any(due_date(t) for t in data["tasks"].get(c["id"], []))]) > 1
    horizon = today + dt.timedelta(days=cfg.get("plan_ahead_days", 90))
    sunday = today + dt.timedelta(days=6 - today.weekday())

    def bucket(d):
        if d < today:
            return "overdue"
        if d <= sunday:
            return "week"
        if d <= horizon:
            return "ahead"
        return None

    rows = {"overdue": [], "week": [], "ahead": []}
    active_classes, term_end, term_end_title = [], None, ""
    overdue_minutes = 0

    sources = [(c, class_code(c["name"])) for c in classes]
    if cfg.get("include_parent_project_tasks", True):
        sources.append((data["parent"], "Program"))

    for project, code in sources:
        tasks = [t for t in data["tasks"].get(project["id"], [])
                 if not t.get("checked") and not t.get("content", "").startswith("* ")]
        by_id = {t["id"]: t for t in tasks}
        kids = {}
        for t in tasks:
            if t.get("parent_id") in by_id:
                kids.setdefault(t["parent_id"], []).append(t)
        for lst in kids.values():
            lst.sort(key=lambda t: t.get("child_order", 0))

        dated = [t for t in tasks if due_date(t)]
        if dated and project is not data["parent"]:
            active_classes.append(project)
            for t in dated:
                end = deadline_date(t) or due_date(t)
                if term_end is None or end > term_end:
                    term_end, term_end_title = end, tidy_title(t["content"])

        for t in dated:
            d = due_date(t)
            parent = by_id.get(t.get("parent_id"))
            b = bucket(d)
            if b is None:
                continue
            meta = []
            if parent:
                pd = due_date(parent)
                # A step gets its own row only when it has its own date and its
                # parent isn't already listed as overdue.
                if pd and (pd == d or bucket(pd) == "overdue"):
                    continue
                own_steps = [k for k in kids[parent["id"]] if due_date(k)]
                idx = own_steps.index(t) + 1 if t in own_steps else None
                label = tidy_title(parent["content"])
                label = re.sub(r"\s*\(.*\)$", "", label)
                meta.append(f"{label} · step {idx} of {len(own_steps)}" if idx and len(own_steps) > 1 else label)
            else:
                k = kind_of(t["content"])
                mins = lecture_minutes(t["content"])
                if k:
                    meta.append(k + (f" · {mins} min" if mins else ""))
                elif mins:
                    meta.append(f"{mins} min")
                if b == "overdue" and k == "Lecture":
                    overdue_minutes += mins
            dur = duration_text(t)
            if dur and not lecture_minutes(t["content"]):
                meta.append(dur)
            if deadline_date(t):
                meta.append("hard deadline")
            open_kids = kids.get(t["id"], [])
            if open_kids and (bucket(d) == "overdue" or all(due_date(k) in (None, d) for k in open_kids)):
                n = len(open_kids)
                meta.append(f"{n} part{'s' if n != 1 else ''} left")
            if multi and code != "Program":
                meta.insert(0, code)
            elif code == "Program":
                meta.insert(0, "Program requirement")
            rows[b].append({
                "date": d,
                "when": fmt_day(d, today),
                "title": tidy_title(t["content"]),
                "meta": " · ".join(m for m in meta if m),
                "late": (today - d).days if b == "overdue" else 0,
                "order": t.get("child_order", 0),
            })

    for b in rows:
        rows[b].sort(key=lambda r: (r["date"], r["order"]))

    # Header details from the first active class.
    overline, subtitle = "", ""
    if active_classes:
        c = active_classes[0]
        cw = current_week(data["sections"].get(c["id"], []), today)
        parts = [class_code(c["name"]) if not multi else " · ".join(class_code(x["name"]) for x in active_classes)]
        if cw and not multi:
            parts += [f"week {cw[0]} of {cw[1]}", cw[2]]
        overline = " · ".join(parts)
        names = ", ".join(class_title(x["name"]) for x in active_classes)
        subtitle = f"{names}."
        if term_end:
            subtitle += f" The term ends {fmt_long(term_end)}, with {plain(term_end_title)}."

    due_today = [r for r in rows["week"] if r["date"] == today]
    rest_week = [r for r in rows["week"] if r["date"] != today]
    oldest = rows["overdue"][0] if rows["overdue"] else None
    days_left = (term_end - today).days if term_end and term_end >= today else None

    stats = [
        (len(rows["overdue"]), "Overdue",
         f"Oldest: {short(oldest['title'])}, due {oldest['date']:%b} {oldest['date'].day}" if oldest else "All caught up"),
        (len(due_today), "Due today", short(due_today[0]["title"]) if due_today else "Nothing due today"),
        (len(rest_week), "Rest of this week", f"Through Sunday, {sunday:%b} {sunday.day}"),
        (days_left if days_left is not None else "—", "Days left in term",
         f"{short(term_end_title)} due {term_end:%a}, {term_end:%b} {term_end.day}" if term_end else ""),
    ]

    overdue_sub = f"{len(rows['overdue'])} item{'s' if len(rows['overdue']) != 1 else ''}"
    if overdue_minutes:
        overdue_sub += f" · {fmt_minutes(overdue_minutes)} of lectures"

    return {
        "overline": overline,
        "subtitle": subtitle,
        "stats": stats,
        "sections": [
            ("overdue", "Overdue", overdue_sub, rows["overdue"], "Nothing overdue right now."),
            ("week", "Due this week", f"{today:%A}, {today:%b} {today.day} – Sunday, {sunday:%b} {sunday.day}",
             rows["week"], "Nothing else due this week."),
            ("ahead", "The plan ahead", "Through the end of term and beyond", rows["ahead"], "Nothing scheduled yet."),
        ],
    }


def plain(title):
    return re.sub(r"\s*\(.*\)$", "", title)


def short(title, n=34):
    t = re.sub(r"^(Watch|Read|Listen|Write)\s+", "", title)
    t = re.sub(r"\s*\(.*\)$", "", t)
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- rendering

CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:Lato,system-ui,sans-serif;color:#333;background:#fff}
.wrap{max-width:1000px;margin:0 auto;padding-left:24px;padding-right:24px}
.topbar{background:#8A2233;color:#fff}
.topbar .wrap{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:4px 16px;padding-top:16px;padding-bottom:16px}
.topbar span{font-size:12px;letter-spacing:2px;text-transform:uppercase}
.topbar .brand{font-weight:700}
.hero{background:#4A0009;color:#fff}
.hero .wrap{padding-top:64px;padding-bottom:56px;display:flex;flex-direction:column;gap:16px}
.overline{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#BF8B3A}
h1{margin:0;font-family:'Cormorant Garamond',Georgia,serif;font-weight:400;font-size:clamp(38px,8vw,60px);line-height:1.05;letter-spacing:1px}
h1 em{text-transform:none}
.hero p{margin:0;font-size:18px;line-height:30px;font-style:italic;max-width:640px}
.stats{background:#F4E8D5}
.stats .wrap{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:24px;padding-top:36px;padding-bottom:36px}
.stat{display:flex;flex-direction:column;gap:6px;border-top:2px solid #BF8B3A;padding-top:14px}
.stat .n{font-family:'Cormorant Garamond',Georgia,serif;font-weight:500;font-size:60px;line-height:1;color:#4A0009}
.stat .label{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#444C54}
.stat .note{font-size:14px;font-style:italic;color:#444C54}
section.list{padding-top:56px}
.list-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 16px;padding-bottom:12px}
h2{margin:0;font-family:'Cormorant Garamond',Georgia,serif;font-weight:500;font-size:34px;letter-spacing:2px;text-transform:uppercase;color:#4A0009}
.list-head .sub{font-size:14px;font-style:italic;color:#5F5F5F}
.rule{width:56px;height:3px;background:#BF8B3A;margin-bottom:4px}
ul.rows{list-style:none;margin:0;padding:0}
.row{display:grid;grid-template-columns:120px minmax(0,1fr) auto;grid-template-areas:"when what badge";align-items:center;gap:8px 24px;padding:16px 0;border-bottom:1px solid #E7E5E2}
.when{grid-area:when;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#444C54}
.what{grid-area:what;display:flex;flex-direction:column;gap:3px}
.title{font-size:17px;line-height:26px;color:#222}
.meta{font-size:13px;color:#5F5F5F}
.badge{grid-area:badge;justify-self:end;background:#8A2233;color:#fff;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:6px 14px;border-radius:20px;white-space:nowrap}
.empty{padding:16px 0;font-style:italic;color:#5F5F5F;border-bottom:1px solid #E7E5E2}
.more{display:none}
footer{background:#4A0009;color:#fff;margin-top:72px}
footer .wrap{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;padding-top:28px;padding-bottom:28px;font-size:13px}
footer .tag{color:#BF8B3A;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;font-size:12px}
@media (max-width:820px){
  .stats .wrap{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:640px){
  .wrap{padding-left:20px;padding-right:20px}
  .topbar .wrap{flex-direction:column;align-items:flex-start}
  .hero .wrap{padding-top:40px;padding-bottom:36px}
  .hero p{font-size:16px;line-height:26px}
  .stats .wrap{gap:20px;padding-top:28px;padding-bottom:28px}
  .stat .n{font-size:46px}
  .stat .note{font-size:13px}
  section.list{padding-top:40px}
  h2{font-size:28px}
  .row{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"when badge" "what what";gap:6px 12px;padding:14px 0}
  .badge{padding:4px 10px;font-size:10px}
  .collapsible:not(.open) .extra{display:none}
  .more{display:flex;align-items:center;justify-content:center;width:100%;min-height:48px;margin-top:16px;border:1px solid #8A2233;border-radius:24px;background:#fff;color:#8A2233;font:700 12px/1 Lato,system-ui,sans-serif;letter-spacing:1.5px;text-transform:uppercase;cursor:pointer}
  footer{margin-top:56px}
}
"""

JS = """
document.querySelectorAll('button.more').forEach(function(b){
  b.addEventListener('click',function(){
    var list=document.getElementById(b.getAttribute('aria-controls'));
    var open=list.classList.toggle('open');
    b.setAttribute('aria-expanded',open?'true':'false');
    b.textContent=open?b.dataset.less:b.dataset.more;
  });
});
"""


def e(s):
    return html.escape(str(s), quote=True)


def render(model, cfg, updated):
    preview = int(cfg.get("mobile_overdue_preview", 5))
    out = []
    out.append(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{e(cfg.get('site_title', 'Seminary accountability'))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;1,400&amp;family=Lato:ital,wght@0,400;0,700;1,400&amp;display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header class="topbar"><div class="wrap">
  <span class="brand">{e(cfg.get('site_title', 'Seminary accountability'))}</span>
  <span>Updated {e(updated)}</span>
</div></header>
<section class="hero"><div class="wrap">
  <span class="overline">{e(model['overline'])}</span>
  <h1>WHERE I <em>stand</em> THIS WEEK</h1>
  <p>{e(model['subtitle'])}</p>
</div></section>
<section class="stats" aria-label="Summary"><div class="wrap">""")
    for n, label, note in model["stats"]:
        out.append(f'<div class="stat"><span class="n">{e(n)}</span><span class="label">{e(label)}</span><span class="note">{e(note)}</span></div>')
    out.append("</div></section>\n<main>")

    for key, title, sub, rows, empty in model["sections"]:
        collapsible = key == "overdue" and len(rows) > preview
        out.append(f'<section class="list wrap" aria-labelledby="h-{key}">')
        out.append(f'<div class="list-head"><h2 id="h-{key}">{e(title)}</h2><span class="sub">{e(sub)}</span></div><div class="rule"></div>')
        if not rows:
            out.append(f'<p class="empty">{e(empty)}</p></section>')
            continue
        out.append(f'<ul class="rows{" collapsible" if collapsible else ""}" id="list-{key}">')
        for i, r in enumerate(rows):
            extra = " extra" if collapsible and i >= preview else ""
            badge = ""
            if r["late"]:
                badge = f'<span class="badge">{r["late"]} day{"s" if r["late"] != 1 else ""} late</span>'
            meta = f'<span class="meta">{e(r["meta"])}</span>' if r["meta"] else ""
            out.append(f'<li class="row{extra}"><span class="when">{e(r["when"])}</span>'
                       f'<div class="what"><span class="title">{e(r["title"])}</span>{meta}</div>{badge}</li>')
        out.append("</ul>")
        if collapsible:
            more = f"Show all {len(rows)} overdue"
            out.append(f'<button type="button" class="more" aria-controls="list-{key}" aria-expanded="false" '
                       f'data-more="{e(more)}" data-less="Show fewer">{e(more)}</button>')
        out.append("</section>")

    out.append(f"""</main>
<footer><div class="wrap">
  <span>Read-only. Refreshed from Todoist several times a day.</span>
  <span class="tag">{e(model['overline'].split(' · ')[0] if model['overline'] else '')}</span>
</div></footer>
<script>{JS}</script>
</body>
</html>
""")
    return "\n".join(out)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site", help="output folder")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--fixture", help="read Todoist data from a JSON file instead of the API")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    now = dt.datetime.now(tz)
    today = dt.date.fromisoformat(args.today) if args.today else now.date()

    if args.fixture:
        data = json.loads(Path(args.fixture).read_text())
    else:
        token = os.environ.get("TODOIST_TOKEN")
        if not token:
            sys.exit("Set the TODOIST_TOKEN environment variable (a GitHub secret in Actions).")
        data = fetch_from_todoist(token, cfg["parent_project"])

    model = build_model(data, today, cfg)
    stamp = now if not args.today else dt.datetime.combine(today, dt.time(6, 0), tz)
    updated = f"{stamp:%a}, {stamp:%b} {stamp.day} · {stamp.strftime('%I:%M %p').lstrip('0')}"

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(model, cfg, updated), encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Wrote {out / 'index.html'}: "
          + ", ".join(f"{len(s[3])} {s[0]}" for s in model["sections"]))


if __name__ == "__main__":
    main()

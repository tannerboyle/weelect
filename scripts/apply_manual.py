"""Apply hand-researched leaders from data/local_manual.json to data/local_officials.json.

Each manual entry names a person and the official-site URLs where we saw them. This script re-opens
those pages in headless Chrome and only promotes the entry to "confirmed" when the person's name sits
next to the title on the page (same check as the other scripts). Failures are reported and skipped.
It replaces the place's/county's officials with the manual entries (unverified Wikidata leftovers,
which are often stale, are dropped). Dev-only: needs playwright + Chrome.
usage: python scripts/apply_manual.py [--chrome /usr/bin/google-chrome] [--only geoid,geoid]
"""
import argparse, json, os, re, sys, datetime, collections
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser(); ap.add_argument("--chrome", default="/usr/bin/google-chrome"); ap.add_argument("--only", default="")
ap.add_argument("--report", default="/tmp/manual_report.json")
A = ap.parse_args()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "local_officials.json")
D = json.load(open(PATH)); M = json.load(open(os.path.join(ROOT, "data", "local_manual.json")))
TODAY = datetime.date.today().isoformat()
SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
TITLES = {"Mayor": ["mayor"], "Mayor-President": ["mayor-president", "mayor president", "mayor"]}

def confirm(name, text, titles):
    t = [w for w in re.sub(r"[^\w .'’-]", " ", name).split() if w.rstrip(".").lower() not in SUFFIX]
    if len(t) < 2: return False
    F, L, T = re.escape(t[0]), re.escape(t[-1]), "|".join(re.escape(x) for x in titles)
    for pat in (rf"\b(?:{T})\b[\s,:\-–—]*(?:[\w.'’\"“”()-]+\s+){{0,4}}?{L}\b", rf"\b{F}\b[^.]{{0,40}}\b{L}\b[\s,.\-–—(]{{1,8}}(?:[\w.]+\s+){{0,3}}(?:{T})\b"):
        for m in re.finditer(pat, text, re.I):
            if any(w in text[max(0, m.start() - 12):m.start()].lower() for w in ("former", "past ", "late ")): continue
            return True
    return False

only = set(filter(None, A.only.split(",")))
report = {}; applied = 0
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=A.chrome, args=["--no-sandbox"])
    pg = b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36").new_page()
    for e in M:
        if only and e["geoid"] not in only: continue
        titles = e.get("match_titles") or TITLES.get(e["title"]) or [e["title"].lower()]
        ok_url = None
        for u in e["urls"]:
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=40000); pg.wait_for_timeout(2500)
                txt = re.sub(r"\s+", " ", pg.inner_text("body"))
                if confirm(e["name"], txt, titles): ok_url = u; break
            except Exception as ex:
                print('  fetch fail', u, str(ex)[:80]); continue
        report[e["geoid"] + ":" + e["name"]] = ok_url
        if not ok_url: continue
        bucket = D["places"] if e["kind"] == "place" else D["counties"]
        rec = bucket.get(e["geoid"])
        if rec is None: print("no such geoid", e["geoid"]); continue
        keep = [o for o in rec["officials"] if o.get("listed_by") == "Official site (checked)" and o["name"] != e["name"] and e["kind"] == "county"]
        rec["officials"] = keep + [{"name": e["name"], "title": e["title"], "party": e.get("party"), "term_start": e.get("term_start"), "term_end": e.get("term_end"),
                                    "status": "confirmed", "source_url": ok_url, "listed_by": "Official site (checked)", "last_verified": TODAY}]
        applied += 1
    b.close()
json.dump(report, open(A.report, "w"), indent=1)
st = collections.Counter()
for kind in ("places", "counties"):
    for v in D[kind].values():
        for o in v["officials"]: st[f"{'county' if kind == 'counties' else 'place'}:{o['status']}"] += 1
D["meta"]["stats"] = dict(sorted(st.items()))
json.dump(D, open(PATH, "w"), indent=1, ensure_ascii=False)
print("applied", applied, "of", len(report)); print("FAILED:", [k for k, v in report.items() if not v])

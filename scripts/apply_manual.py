"""Apply hand-researched leaders from data/local_manual.json to data/local_officials.json.

Each manual entry names a person and the official-site URLs where we saw them. This script re-opens
those pages in headless Chrome and only promotes the entry to "confirmed" when the person's name sits
next to the title on the page (same check as the other scripts). Failures are reported and skipped.
It replaces the place's/county's officials with the manual entries (unverified Wikidata leftovers,
which are often stale, are dropped). Dev-only: needs playwright + Chrome.
usage: python scripts/apply_manual.py [--chrome /usr/bin/google-chrome] [--only geoid,geoid]
"""
import argparse, json, os, re, sys, datetime, collections

ap = argparse.ArgumentParser(); ap.add_argument("--chrome", default="/usr/bin/google-chrome"); ap.add_argument("--only", default="")
ap.add_argument("--report", default="/tmp/manual_report.json"); ap.add_argument("--shard", default="0/1"); ap.add_argument("--merge", nargs="*", help="merge shard report files into data/local_officials.json (no browser)"); ap.add_argument("--gaz", default="/tmp/loc/gaz_counties.txt")
A = ap.parse_args()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "local_officials.json")
D = json.load(open(PATH)); M = json.load(open(os.path.join(ROOT, "data", "local_manual.json")))
TODAY = datetime.date.today().isoformat()
SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
def title_set(title):
    t = title.lower()
    if "mayor-president" in t: return ["mayor-president", "mayor president", "mayor"]
    if "mayor" in t: return ["mayor"]
    if "judge" in t: return ["county judge", "judge/executive", "judge executive"]
    if "chair" in t or "president" in t: return ["chair", "chairman", "chairwoman", "chairperson", "board president", "president of the board"]
    if "executive" in t: return ["county executive", "executive"]
    return [t]
KEY = re.compile(r"(mayor|judge|executive|chair|supervisors|commissioners?|board|leadership|elected|council|president)", re.I)

def confirm(name, text, titles):
    t = [w for w in re.sub(r"[^\w .'’-]", " ", name).split() if w.rstrip(".").lower() not in SUFFIX]
    if len(t) < 2: return False
    F, L, T = re.escape(t[0]), re.escape(t[-1]), "|".join(re.escape(x) for x in titles)
    for pat in (rf"\b(?:{T})\b[\s,:\-–—]*(?:[\w.'’\"“”()-]+\s+){{0,4}}?{L}\b", rf"\b{F}\b[^.]{{0,40}}\b{L}\b[\s,.\-–—(]{{1,8}}(?:[\w.]+\s+){{0,3}}(?:{T})\b"):
        for m in re.finditer(pat, text, re.I):
            if any(w in text[max(0, m.start() - 12):m.start()].lower() for w in ("former", "past ", "late ", "deputy", "vice", "assistant", "associate")): continue
            if re.search(r"deputy|vice|assistant|associate|pro tem", m.group(0), re.I): continue
            return True
    return False

only = set(filter(None, A.only.split(",")))
import csv

def merge(files):
    gaz = {}
    if os.path.exists(A.gaz):
        for r in csv.DictReader(open(A.gaz, encoding="latin-1"), delimiter="\t"):
            r = {k.strip(): v for k, v in r.items()}; gaz[r["GEOID"]] = (r["NAME"], r["USPS"])
    ents = {e["geoid"] + ":" + e["name"]: e for e in M}; applied = 0
    for f in files:
        for key, ok_url in json.load(open(f)).items():
            if not ok_url or key not in ents: continue
            e = ents[key]; bucket = D["places"] if e["kind"] == "place" else D["counties"]
            rec = bucket.get(e["geoid"])
            if rec is None:
                if e["kind"] == "county" and e["geoid"] in gaz: rec = bucket[e["geoid"]] = {"name": gaz[e["geoid"]][0], "state": gaz[e["geoid"]][1], "officials": []}
                else: print("no such geoid", e["geoid"]); continue
            rec["officials"] = [{"name": e["name"], "title": e["title"], "party": e.get("party"), "term_start": e.get("term_start"), "term_end": e.get("term_end"),
                                 "status": "confirmed", "source_url": ok_url, "listed_by": "Official site (checked)", "last_verified": TODAY}]
            applied += 1
    st = collections.Counter()
    for kind in ("places", "counties"):
        for v in D[kind].values():
            for o in v["officials"]: st[f"{'county' if kind == 'counties' else 'place'}:{o['status']}"] += 1
    D["meta"]["stats"] = dict(sorted(st.items()))
    json.dump(D, open(PATH, "w"), indent=1, ensure_ascii=False); print("applied", applied, dict(st))

if A.merge:
    merge(A.merge); sys.exit()
from playwright.sync_api import sync_playwright
si, sn = map(int, A.shard.split("/"))
report = {}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=A.chrome, args=["--no-sandbox"])
    ctx = b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
    for ei, e in enumerate(M):
        if ei % sn != si: continue
        if only and e["geoid"] not in only: continue
        titles = e.get("match_titles") or title_set(e["title"])
        ok_url = None; first_links = None; first_host = None
        pg = None
        for u in e["urls"]:
            try:
                if pg: pg.close()
                pg = ctx.new_page()
                pg.goto(u, wait_until="domcontentloaded", timeout=40000); pg.wait_for_timeout(2500)
                txt = re.sub(r"\s+", " ", pg.inner_text("body"))
                if confirm(e["name"], txt, titles): ok_url = u; break
                if first_links is None:
                    first_links = pg.eval_on_selector_all("a", "els=>els.map(e=>[e.href,(e.innerText||'').trim()])")
                    first_host = pg.url.split("/")[2]
            except Exception as ex:
                print('  fetch fail', u, str(ex)[:80]); continue
        if not ok_url and first_links and e.get("crawl", True):   # one hop: leadership / board / judge pages on the same site
            subs = []
            for h, t in first_links:
                if h.startswith("http") and h.split("/")[2] == first_host and KEY.search(h + " " + t) and h not in subs and not h.lower().endswith((".pdf", ".jpg")): subs.append(h)
            subs.sort(key=lambda h: (0 if re.search(r'mayor', h, re.I) else 1))
            for u in subs[:12]:
                try:
                    pg.close(); pg = ctx.new_page()
                    pg.goto(u, wait_until="domcontentloaded", timeout=30000); pg.wait_for_timeout(1500)
                    if confirm(e["name"], re.sub(r"\s+", " ", pg.inner_text("body")), titles): ok_url = u; break
                except Exception: continue
        report[e["geoid"] + ":" + e["name"]] = ok_url
        json.dump(report, open(A.report, "w"), indent=1); print(("OK   " if ok_url else "FAIL ") + e["name"], flush=True)
    b.close()
print("shard done", sum(1 for v in report.values() if v), "of", len(report))

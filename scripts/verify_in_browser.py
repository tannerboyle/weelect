"""Second verification pass for large places whose city site blocked the plain HTTP check.

Opens each still-unverified mayor's cited government page (and the city's own site) in a real
headless Chrome and applies the same name-next-to-title check as build_local_officials.py.
Promotes matches to "confirmed". Needs playwright + a Chrome binary (dev-only; not used by the app).
usage: python scripts/verify_in_browser.py [--min-pop 60000] [--shard i/n]
"""
import argparse, json, os, re, sys, datetime
from urllib.parse import urlparse

ap = argparse.ArgumentParser(); ap.add_argument("--min-pop", type=int, default=60000); ap.add_argument("--shard", default="0/1")
ap.add_argument("--apply", nargs="*", help="merge shard result files into data/local_officials.json and exit"); ap.add_argument("--chrome", default="/usr/bin/google-chrome"); ap.add_argument("--out", default=None)
A = ap.parse_args()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "local_officials.json")
D = json.load(open(PATH)); TODAY = datetime.date.today().isoformat()
SUFFIX = {"jr", "sr", "ii", "iii", "iv"}

def confirm(name, text, titles=("mayor",)):
    t = [w for w in re.sub(r"[^\w .'’-]", " ", name).split() if w.rstrip(".").lower() not in SUFFIX]
    if len(t) < 2: return False
    F, L, T = re.escape(t[0]), re.escape(t[-1]), "|".join(titles)
    for pat in (rf"\b(?:{T})\b[\s,:\-–—]*(?:[\w.'’\"“”()-]+\s+){{0,4}}?{L}\b", rf"\b{F}\b[^.]{{0,40}}\b{L}\b[\s,.\-–—(]{{1,8}}(?:[\w.]+\s+){{0,3}}(?:{T})\b"):
        for m in re.finditer(pat, text, re.I):
            if any(w in text[max(0, m.start() - 12):m.start()].lower() for w in ("former", "past ", "late ")): continue
            return True
    return False

if A.apply:
    n_ok = 0
    for f in A.apply:
        for key, url in json.load(open(f)).items():
            g, k = key.split(":"); o = D["places"][g]["officials"][int(k)]
            if o["status"] == "unverified": o.update(status="confirmed", source_url=url, last_verified=TODAY); n_ok += 1
    for v in D["places"].values():   # two different people both "confirmed" (e.g. a former mayor still on a page): show neither
        sh = [o for o in v["officials"] if o["status"] != "unverified"]
        if len({o["name"] for o in sh}) > 1:
            for o in sh: o["status"] = "unverified"
    st = {}
    for kind in ("places", "counties"):
        for v in D[kind].values():
            for o in v["officials"]: st[f"{'county' if kind=='counties' else kind[:-1]}:{o['status']}"] = st.get(f"{'county' if kind=='counties' else kind[:-1]}:{o['status']}", 0) + 1
    D["meta"]["stats"] = dict(sorted(st.items()))
    json.dump(D, open(PATH, "w"), indent=1, ensure_ascii=False); print("promoted", n_ok, st); sys.exit()
i, n = map(int, A.shard.split("/"))
todo = [(g, v) for g, v in sorted(D["places"].items()) if v.get("pop", 0) >= A.min_pop and any(o["status"] == "unverified" for o in v["officials"])]
todo = todo[i::n]; res = {}
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=A.chrome, args=["--no-sandbox"])
    ctx = b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"); pg = ctx.new_page()
    for g, v in todo:
        for k, o in enumerate(v["officials"]):
            if o["status"] != "unverified" or o["title"] != "Mayor": continue
            for u in [o["source_url"]]:
                try:
                    pg.goto(u, wait_until="domcontentloaded", timeout=40000); pg.wait_for_timeout(2500)
                    txt = re.sub(r"\s+", " ", pg.inner_text("body"))
                    links = pg.eval_on_selector_all("a", "els=>els.map(e=>[e.href,e.innerText])")
                    ok = confirm(o["name"], txt)
                    if not ok:   # one hop: a mayor / leadership sub-page on the same host
                        subs = [h for h, t in links if urlparse(h).hostname == urlparse(u).hostname and re.search(r"mayor|leadership|elected", h + t, re.I)][:3]
                        for s in subs:
                            pg.goto(s, wait_until="domcontentloaded", timeout=40000); pg.wait_for_timeout(2000)
                            if confirm(o["name"], re.sub(r"\s+", " ", pg.inner_text("body"))): ok, u = True, s; break
                    if ok: res[f"{g}:{k}"] = u
                except Exception as e:
                    pass
    b.close()
out = A.out or f"/tmp/browser_verify_{i}.json"
json.dump(res, open(out, "w")); print("shard", i, "checked", len(todo), "confirmed", len(res))

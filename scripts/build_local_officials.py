"""Build data/local_officials.json from free/open sources (no paid APIs, no per-lookup cost).

Sources
  - openstates/people (municipalities/*.yml)  : mayors with official source URLs and term end dates (CC0)
  - Wikidata (SPARQL)                         : head of government of US places (FIPS 55 code), official websites
  - Wikipedia infobox (MediaWiki API)         : leader of the ~500 most populous counties
  - Census Gazetteer                          : place / county names for GEOIDs
Every entry is then checked against a government web page we actually fetch (auto-check): the person's
name must appear next to their title on a page from the cited site. Entries that cannot be confirmed are
kept but marked "unverified" and are NOT shown on the site.

Usage: python scripts/build_local_officials.py [--cache /tmp/loc] [--skip-fetch]
Needs: requests, pyyaml (see requirements) and `git`.
"""
import argparse, collections, concurrent.futures as cf, csv, datetime, hashlib, io, json, os, re, subprocess, sys, tempfile, zipfile
from urllib.parse import urljoin, urlparse
import requests, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = datetime.date.today().isoformat()
ap = argparse.ArgumentParser(); ap.add_argument("--cache", default="/tmp/loc"); ap.add_argument("--skip-fetch", action="store_true")
ARGS = ap.parse_args()
CACHE = ARGS.cache; os.makedirs(CACHE + "/pages", exist_ok=True)
WD_UA = {"User-Agent": "weelect-local-research/1.0 (https://weelect.onrender.com)"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 (We Elect civic-data check)"}
STATE_FIPS = {"01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE","11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH","40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD","47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY"}
ST2FIPS = {v: k for k, v in STATE_FIPS.items()}

def sparql(name, q):
    p = f"{CACHE}/wd_{name}.json"
    if not os.path.exists(p):
        r = requests.post("https://query.wikidata.org/sparql", data={"query": q}, headers={**WD_UA, "Accept": "application/sparql-results+json"}, timeout=300)
        r.raise_for_status(); open(p, "w").write(r.text)
    return json.load(open(p))["results"]["bindings"]
V = lambda r, k: r.get(k, {}).get("value")

# ---------------- Census gazetteer (names) ----------------
def gazetteer(kind):
    p = f"{CACHE}/gaz_{kind}.txt"
    if not os.path.exists(p):
        z = requests.get(f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_{kind}_national.zip", timeout=120).content
        zf = zipfile.ZipFile(io.BytesIO(z)); open(p, "w").write(zf.read(zf.namelist()[0]).decode("latin-1"))
    rows = list(csv.DictReader(open(p, encoding="latin-1"), delimiter="\t"))
    return {r["GEOID"].strip(): {"name": r["NAME"].strip(), "state": r["USPS"].strip()} for r in (dict((k.strip(), v) for k, v in x.items()) for x in rows)}
GAZ_P, GAZ_C = gazetteer("place"), gazetteer("counties")

def slug(s):
    s = re.sub(r"\b(city|town|village|borough|township|municipality|cdp|urban county|consolidated government|metropolitan government|unified government)\b", " ", s.lower())
    s = re.sub(r"\(balance\)|\(.*?\)", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)

# ---------------- Wikidata: place heads of government ----------------
Q_HEADS = '''SELECT ?item ?itemLabel ?fips ?pop ?person ?personLabel ?start ?end ?rank ?partyLabel ?ref WHERE {
  ?item wdt:P774 ?fips . ?item p:P6 ?stmt . ?stmt ps:P6 ?person . ?stmt wikibase:rank ?rank .
  OPTIONAL { ?stmt pq:P580 ?start } OPTIONAL { ?stmt pq:P582 ?end } OPTIONAL { ?person wdt:P102 ?party }
  OPTIONAL { ?stmt prov:wasDerivedFrom/pr:P854 ?ref } OPTIONAL { ?item wdt:P1082 ?pop }
  FILTER(?rank != wikibase:DeprecatedRank) SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }'''
Q_SITES = 'SELECT ?item ?site WHERE { ?item wdt:P774 ?f . ?item wdt:P856 ?site . }'
Q_COUNTIES = '''SELECT ?item ?itemLabel ?fips ?pop ?site ?enwiki WHERE { ?item wdt:P882 ?fips . FILTER(STRLEN(?fips)=5)
  ?item wdt:P31/wdt:P279* wd:Q47168 . OPTIONAL { ?item wdt:P1082 ?pop } OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?enwiki schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }'''

def geoid_from_fips55(f):
    f = f.replace("-", "")
    return f if len(f) == 7 else None

def clean_name(n):
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", n or "")).strip()

def party_short(p):
    if isinstance(p, list):
        p = (p[0].get("party") if p and isinstance(p[0], dict) else (p[0] if p else "")) or ""
    p = (p or "").replace(" Party", "").replace(" (United States)", "").strip()
    return {"Democratic": "Democratic", "Republican": "Republican", "Independent": "Independent", "Libertarian": "Libertarian", "Green": "Green"}.get(p, p) or None

cands = collections.defaultdict(list)   # geoid -> [candidate dict]
POP = {}
rows = sparql("heads", Q_HEADS); by_item = collections.defaultdict(list)
for r in rows: by_item[V(r, "item")].append(r)
sites_rows = sparql("sites", Q_SITES); SITE = {}
for r in sites_rows: SITE.setdefault(V(r, "item"), V(r, "site"))
for item, rs in by_item.items():
    gid = geoid_from_fips55(V(rs[0], "fips"))
    if not gid or gid not in GAZ_P: continue
    cur = [r for r in rs if not V(r, "end")]
    if not cur: continue
    POP[gid] = max([float(V(r, "pop")) for r in rs if V(r, "pop")] or [0])
    persons = collections.OrderedDict()
    for r in cur:
        d = persons.setdefault(V(r, "person"), {"name": clean_name(V(r, "personLabel")), "party": None, "start": None, "refs": set(), "preferred": False, "qid": V(r, "person").rsplit("/", 1)[-1]})
        if V(r, "partyLabel") and not d["party"]: d["party"] = party_short(V(r, "partyLabel"))
        if V(r, "start"): d["start"] = max(d["start"] or "", V(r, "start")[:10])
        if V(r, "ref"): d["refs"].add(V(r, "ref"))
        if V(r, "rank").endswith("PreferredRank"): d["preferred"] = True
    pl = list(persons.values())
    if len(pl) > 1:   # several "current" heads: keep a preferred one, else the latest start; ambiguous -> skip
        pref = [p for p in pl if p["preferred"]]
        if len(pref) == 1: pl = pref
        else:
            dated = sorted([p for p in pl if p["start"]], key=lambda p: p["start"])
            if dated and (len(dated) == 1 or dated[-1]["start"] != dated[-2]["start"]): pl = [dated[-1]]
            else: continue
    p = pl[0]
    if re.match(r"^Q\d+$", p["name"]): continue
    cands[gid].append({"src": "wikidata", "name": p["name"], "party": p["party"], "start": p["start"], "end": None, "refs": sorted(p["refs"]),
                       "site": SITE.get(item), "qid": p["qid"], "item": item.rsplit("/", 1)[-1]})
print("wikidata place candidates:", len(cands))

# ---------------- openstates mayors ----------------
tmp = tempfile.mkdtemp(); subprocess.check_call(["git", "clone", "--depth", "1", "-q", "https://github.com/openstates/people.git", tmp])
by_slug = collections.defaultdict(list)
for gid, g in GAZ_P.items():
    if g["state"] in ST2FIPS and not g["name"].endswith("CDP"): by_slug[(g["state"], slug(g["name"]))].append(gid)
os_n = os_unmapped = 0
base = os.path.join(tmp, "data")
for st in sorted(os.listdir(base)):
    d = os.path.join(base, st, "municipalities")
    if st == "us" or not os.path.isdir(d): continue
    for fn in sorted(os.listdir(d)):
        p = yaml.safe_load(open(os.path.join(d, fn)))
        for r in p.get("roles") or []:
            if r.get("type") != "mayor": continue
            end = str(r.get("end_date") or "")
            if end and end < TODAY: continue
            m = re.search(r"place:([^/]+)/", r.get("jurisdiction", ""))
            if not m: continue
            ids = by_slug.get((st.upper(), slug(m.group(1).replace("_", " "))))
            if not ids: os_unmapped += 1; continue
            if len(ids) > 1:   # prefer a place Wikidata also knows
                ids = [i for i in ids if i in cands] or ids[:1]
            os_n += 1
            cands[ids[0]].append({"src": "openstates", "name": clean_name(p["name"]), "party": party_short(p.get("party")), "start": str(r.get("start_date") or "") or None,
                                  "end": end or None, "refs": [s["url"] for s in p.get("sources") or [] if s.get("url")], "site": None, "osid": p["id"]})
print("openstates mayor roles mapped:", os_n, "unmapped:", os_unmapped)

# ---------------- page fetch + name check ----------------
OFFICIAL_RE = re.compile(r"(\.gov$|\.gov\.|\.us$|\.us/|\.mil$|city|town|village|borough|county|twp|township|co\.|ci\.|cityof|townof|villageof|mayor|council|commission|parish|\.org$|\.com$)", re.I)
BAD_HOST = re.compile(r"(wikipedia|wikidata|facebook|twitter|x\.com|instagram|youtube|linkedin|nbc|cbs|abc|fox|cnn|nytimes|patch\.com|ballotpedia|vote411|usmayors|news|times|tribune|post|gazette|herald|journal|press|ledger|sentinel|observer)", re.I)
def host(u): return (urlparse(u).hostname or "").lower()
def gov_like(u):
    h = host(u)
    return bool(h) and not BAD_HOST.search(h) and bool(re.search(r"(\.gov$|\.gov\.|\.us$|\.mil$|\.state\.)", h) or re.search(r"(city|town|village|borough|county|twp|township|ci\.|co\.|parish|metro|mayor)", h) or h.startswith("www.") and h.count(".") == 2 and h.endswith(".org") and re.search(r"(city|town|co)", h))

def fetch_text(url):
    f = f"{CACHE}/pages/{hashlib.sha1(url.encode()).hexdigest()}.json"
    if os.path.exists(f): return json.load(open(f))
    out = {"url": url, "status": None, "text": "", "links": []}
    if not ARGS.skip_fetch:
        try:
            r = requests.get(url, headers=BROWSER_UA, timeout=14, stream=True, allow_redirects=True)
            out["status"] = r.status_code; out["final"] = r.url
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and ("html" in ct or "text" in ct or not ct):
                raw = r.raw.read(1_500_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
                links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.I | re.S)
                out["links"] = [[urljoin(r.url, h), re.sub(r"<[^>]+>", " ", t)[:80]] for h, t in links if re.search(r"(mayor|judge|executive|supervisor|chair|commission|board)", h + t, re.I)][:12]
                t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw); t = re.sub(r"<[^>]+>", " ", t)
                import html; out["text"] = re.sub(r"\s+", " ", html.unescape(t))[:400000]
        except Exception as e:
            out["status"] = "ERR " + type(e).__name__
    json.dump(out, open(f, "w")); return out

SUFFIX = {"jr", "sr", "ii", "iii", "iv", "md", "dds", "phd"}
def name_parts(n):
    t = [w for w in re.sub(r"[^\w .'’-]", " ", n).split() if w.rstrip(".").lower() not in SUFFIX]
    return (t[0], t[-1]) if len(t) >= 2 else (None, None)

def confirm(name, text, titles):
    """True if the page shows `name` next to one of `titles` (not 'former')."""
    first, last = name_parts(name)
    if not last: return False
    T = "|".join(titles); L = re.escape(last); F = re.escape(first)
    pats = [rf"\b(?:{T})\b[\s,:\-–—]*(?:[\w.'’\"“”()-]+\s+){{0,4}}?{L}\b",
            rf"\b{F}\b[^.]{{0,40}}\b{L}\b[\s,.\-–—(]{{1,8}}(?:[\w.]+\s+){{0,3}}(?:{T})\b"]
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            before = text[max(0, m.start() - 12):m.start()].lower()
            if "former" in before or "past " in before or "late " in before: continue
            return True
    return False

def verify(cand, titles, pages):
    """Try cited government pages, the place's site, and its mayor/executive sub-links."""
    urls = []
    for u in cand["refs"]:
        if gov_like(u): urls.append(u)
    if cand.get("site"): urls.append(cand["site"])
    seen = set(); tried = []
    queue = [u for u in urls if not (u in seen or seen.add(u))][:3]
    for u in list(queue):
        pg = fetch_text(u); tried.append(u)
        if pg["text"] and confirm(cand["name"], pg["text"], titles): return u
        for lu, _ in pg["links"][:4]:
            if lu not in seen and (gov_like(lu) or host(lu) == host(u)): seen.add(lu); queue.append(lu)
    for u in queue[len(urls):][:6]:
        pg = fetch_text(u)
        if pg["text"] and confirm(cand["name"], pg["text"], titles): return u
    return None

# ---------------- counties (Wikipedia infobox, top ~500 by population) ----------------
def wikilink_clean(s):
    s = re.sub(r"<ref.*?</ref>|<ref[^>]*/>|\{\{efn.*?\}\}|<!--.*?-->|<br\s*/?>", " ", s, flags=re.S)
    s = re.sub(r"\{\{[^{}]*\}\}", " ", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    return re.sub(r"\s+", " ", s).strip(" ,;")
TITLE_OK = re.compile(r"(county judge|county executive|county mayor|county president|board president|chair|executive|mayor|county manager|county commission(?:er)? president|president of the board|administrator)", re.I)
def county_infobox(title):
    r = requests.get("https://en.wikipedia.org/w/api.php", params={"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main", "rvsection": 0, "titles": title, "format": "json", "formatversion": 2}, headers=WD_UA, timeout=30).json()
    try: s = r["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
    except Exception: return []
    kv = dict((m.group(1).strip().lower(), m.group(2)) for m in re.finditer(r"^\s*\|\s*([a-z_0-9 ]+?)\s*=\s*(.*)$", s, re.M))
    out = []
    for k, v in kv.items():
        m = re.fullmatch(r"leader_title(\d*)", k)
        if not m: continue
        n = kv.get(f"leader_name{m.group(1)}", "")
        title_c, name_c = wikilink_clean(v), wikilink_clean(n)
        if not name_c or "{{" in n and "collapsible" in n.lower(): continue
        pm = re.search(r"\((D|R|I)\)|\b(Democratic|Republican)\b", name_c)
        party = {"D": "Democratic", "R": "Republican", "I": "Independent"}.get(pm.group(1)) if pm and pm.group(1) else (pm.group(2) if pm else None)
        name_c = clean_name(re.sub(r"\b(Democratic|Republican)\b", "", name_c))
        if TITLE_OK.search(title_c) and 2 <= len(name_c.split()) <= 5 and not re.search(r"vacant|none|tbd", name_c, re.I):
            out.append({"title": title_c, "name": name_c, "party": party})
    return out

crows = sparql("counties", Q_COUNTIES); cinfo = {}
for r in crows:
    g = V(r, "fips")
    if g in GAZ_C: cinfo.setdefault(g, {"pop": 0, "site": None, "wiki": None}); c = cinfo[g]; c["pop"] = max(c["pop"], float(V(r, "pop") or 0)); c["site"] = c["site"] or V(r, "site"); c["wiki"] = c["wiki"] or V(r, "enwiki")
top = sorted(cinfo.items(), key=lambda kv: -kv[1]["pop"])[:500]
county_cands = {}
def do_county(kv):
    g, c = kv
    if not c["wiki"]: return g, []
    p = f"{CACHE}/cinfo_{g}.json"
    if os.path.exists(p): return g, json.load(open(p))
    try: res = county_infobox(c["wiki"].rsplit("/wiki/", 1)[-1].replace("_", " ").replace("%2C", ","))
    except Exception: res = []
    json.dump(res, open(p, "w")); return g, res
with cf.ThreadPoolExecutor(6) as ex:
    for g, res in ex.map(do_county, top):
        if res: county_cands[g] = res
print("counties with an infobox leader:", len(county_cands), "of", len(top))

# ---------------- verify + assemble ----------------
def title_words(title): return [w for w in re.split(r"[^a-z]+", title.lower()) if len(w) > 3 and w not in ("county", "board", "chair")] or ["chair"]
jobs = []
for gid, cs in cands.items():
    for c in cs: jobs.append(("place", gid, c, ["mayor"], "Mayor"))
for gid, res in county_cands.items():
    for c in res:
        cc = {"src": "wikipedia", "name": c["name"], "party": c["party"], "start": None, "end": None, "refs": [], "site": cinfo[gid]["site"], "title": c["title"]}
        jobs.append(("county", gid, cc, [re.escape(c["title"].lower().replace("the ", ""))] + [re.escape(w) for w in title_words(c["title"])] + ["judge", "executive", "chair", "chairman", "chairwoman", "chairperson", "president", "mayor", "commissioner"], c["title"]))
def run(job):
    kind, gid, c, titles, title = job
    try: return job, verify(c, titles, None)
    except Exception as e: return job, None
done = []
with cf.ThreadPoolExecutor(24) as ex:
    for i, res in enumerate(ex.map(run, jobs)):
        done.append(res)
        if i % 400 == 0: print("verified", i, "/", len(jobs), flush=True)

def norm_name(n):
    f, l = name_parts(n); return (re.sub(r"[^a-z]", "", (f or "")[:1].lower()), re.sub(r"[^a-z]", "", (l or "").lower()))
out = {"places": {}, "counties": {}}
stats = collections.Counter()
by_geo = collections.defaultdict(list)
for (kind, gid, c, titles, title), conf in done: by_geo[(kind, gid)].append((c, conf, title))
for (kind, gid), lst in by_geo.items():
    entries = []
    names = {norm_name(c["name"]) for c, _, _ in lst}
    for c, conf, title in lst:
        agree = sum(1 for c2, _, _ in lst if norm_name(c2["name"]) == norm_name(c["name"]) and c2["src"] != c["src"])
        status = "confirmed" if conf else ("corroborated" if agree else "unverified")
        srcs = [conf] if conf else [u for u in c["refs"] if gov_like(u)][:1] or ([c["site"]] if c.get("site") else []) or ([f"https://www.wikidata.org/wiki/{c['qid']}"] if c.get("qid") else [])
        if not srcs and c["src"] == "wikipedia": srcs = [f"https://en.wikipedia.org/wiki/{cinfo[gid]['wiki'].rsplit('/wiki/', 1)[-1]}"]
        if not srcs: continue
        label = {"wikidata": "Wikidata", "openstates": "Open States", "wikipedia": "Wikipedia"}[c["src"]]
        entries.append({"name": c["name"], "title": title, "party": c["party"], "term_start": c["start"], "term_end": c["end"], "status": status,
                        "source_url": srcs[0], "listed_by": label, "last_verified": TODAY})
    # merge duplicates (same person from two datasets): keep the strongest status, union of listed_by
    merged = {}
    rank = {"confirmed": 3, "corroborated": 2, "unverified": 1}
    for e in entries:
        k = norm_name(e["name"]) + (e["title"],)
        if k in merged:
            m = merged[k]
            if rank[e["status"]] > rank[m["status"]]: e, m = {**e, "listed_by": m["listed_by"]}, e
            m = e if rank[e["status"]] >= rank[m["status"]] else m
            m["listed_by"] = " + ".join(sorted(set(m["listed_by"].split(" + ") + e["listed_by"].split(" + "))))
            for kk in ("party", "term_start", "term_end"): m[kk] = m.get(kk) or e.get(kk)
            merged[k] = m
        else: merged[k] = e
    es = list(merged.values())
    if kind == "place" and len({norm_name(e["name"]) for e in es if e["status"] != "unverified"}) > 1:
        top_s = max(rank[e["status"]] for e in es)    # disagreement: keep only the best-supported person(s)
        es = [e for e in es if rank[e["status"]] == top_s]
        if len({norm_name(e["name"]) for e in es}) > 1: es = [dict(e, status="unverified") for e in es]
    g = (GAZ_P if kind == "place" else GAZ_C)[gid]
    out["places" if kind == "place" else "counties"][gid] = {"name": g["name"], "state": g["state"], **({"pop": int(POP[gid])} if kind == "place" and POP.get(gid) else {}), "officials": es}
    for e in es: stats[(kind, e["status"])] += 1
meta = {"generated": TODAY, "last_verified": TODAY,
        "status_key": {"confirmed": "Name and title found on a page from the cited government site when we checked (automated check).",
                       "corroborated": "Two independent open datasets list the same person; we could not open an official page to confirm.",
                       "unverified": "Listed by a single open dataset and not confirmed; not shown on the site."},
        "sources": ["openstates/people (CC0)", "Wikidata (CC0)", "Wikipedia infobox (CC BY-SA, used for names only)", "US Census Gazetteer"],
        "stats": {f"{k}:{s}": n for (k, s), n in sorted(stats.items())}}
json.dump({"meta": meta, **out}, open(os.path.join(ROOT, "data", "local_officials.json"), "w"), indent=1, ensure_ascii=False)
print(json.dumps(meta["stats"], indent=1))

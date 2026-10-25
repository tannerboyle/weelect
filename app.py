"""weelect.org prototype: address -> elected officials, plus profile pages.

Run:  .venv/bin/python app.py   (http://127.0.0.1:5000)
Optional env vars (see README): CONGRESS_API_KEY, OPENSTATES_API_KEY
"""
import json, os, re, time
import votes as votes_mod
from functools import lru_cache
import requests
import datetime
from markupsafe import Markup
from flask import Flask, render_template, request, abort

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CONGRESS_KEY = os.environ.get("CONGRESS_API_KEY", "").strip()
OPENSTATES_KEY = os.environ.get("OPENSTATES_API_KEY", "").strip()
# "ACS2025_Current" = 119th Congress districts (who is seated today).
# "Current_Current" already returns the 120th Congress map (starts Jan 2027), which is wrong for sitting members.
CENSUS_VINTAGE = os.environ.get("CENSUS_VINTAGE", "ACS2025_Current")
UA = {"User-Agent": "weelect-prototype/0.1"}

app = Flask(__name__)


@app.context_processor
def _globals():
    return {"today": datetime.date.today().strftime("%A, %B %-d, %Y")}


@lru_cache(maxsize=128)
def state_svg(code):
    """Inline SVG (so CSS can colour it) of the state/territory outline from static/states/, or None."""
    p = os.path.join(ROOT, "static", "states", f"{code}.svg")
    if not re.fullmatch(r"[A-Z]{2}", code or "") or not os.path.exists(p):
        return None
    return Markup(open(p).read())


def flag_url(code):
    if re.fullmatch(r"[A-Z]{2}", code or ""):
        for ext in ("svg", "webp"):   # simple flags stay SVG; very detailed ones are 480px WebP
            if os.path.exists(os.path.join(ROOT, "static", "flags", f"{code}.{ext}")):
                return f"/static/flags/{code}.{ext}"
    return None


@app.route("/credits")
def credits():
    flags = _load("flag_credits.json")
    return render_template("credits.html", flags=flags)


# ---------- data loading ----------
def _load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)

LEGS = _load("legislators-current.json")
SOCIAL = {s["id"]["bioguide"]: s["social"] for s in _load("legislators-social-media.json") if "bioguide" in s["id"]}
STATE_PEOPLE = _load("state_people.json")
STATE_BY_ID = {p["id"]: p for p in STATE_PEOPLE}
FED_BY_ID = {l["id"]["bioguide"]: l for l in LEGS}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower().lstrip("0") if str(s).isdigit() or str(s).lstrip("0").isdigit() else str(s).lower())


def place_slug(name):
    return re.sub(r"\s+", "_", re.sub(r"[^a-z0-9 ]", "", name.lower()).strip())


# ---------- geocoding ----------
@lru_cache(maxsize=512)
def census_geocode(address):
    r = requests.get(
        "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress",
        params={"address": address, "benchmark": "Public_AR_Current", "vintage": CENSUS_VINTAGE,
                "layers": "all", "format": "json"},
        headers=UA, timeout=25)
    r.raise_for_status()
    matches = r.json()["result"]["addressMatches"]
    if not matches:
        return None
    m = matches[0]
    g = m["geographies"]

    def first(prefix_contains, exclude=None):
        for k, v in g.items():
            if prefix_contains in k and v:
                return v[0]
        return None

    return {
        "matched": m["matchedAddress"], "lat": m["coordinates"]["y"], "lng": m["coordinates"]["x"],
        "state": g["States"][0]["STUSAB"], "state_name": g["States"][0]["NAME"],
        "county": (g.get("Counties") or [{}])[0].get("NAME"),
        "place": (g.get("Incorporated Places") or [{}])[0].get("BASENAME"),
        "place_full": (g.get("Incorporated Places") or [{}])[0].get("NAME"),
        "cd_geoid": (first("Congressional Districts") or {}).get("GEOID"),
        "cd_label": (first("Congressional Districts") or {}).get("NAME"),
        "sldu": (first("State Legislative Districts - Upper") or {}).get("BASENAME"),
        "sldl": (first("State Legislative Districts - Lower") or {}).get("BASENAME"),
    }


# ---------- federal ----------
def fed_photo(bio):
    """Local copy from static/photos (downloaded by scripts/fetch_data.py), or None."""
    if os.path.exists(os.path.join(ROOT, "static", "photos", bio + ".jpg")):
        return f"/static/photos/{bio}.jpg"
    return None  # ~15 members have no photo in the source set


def fed_summary(l):
    t = l["terms"][-1]
    chamber = "U.S. Senator" if t["type"] == "sen" else ("Resident Commissioner" if t["state"] == "PR" else (
        "U.S. Delegate" if t["state"] in ("DC", "GU", "VI", "AS", "MP") else "U.S. Representative"))
    where = t["state"] if t["type"] == "sen" else (
        f'{t["state"]}-{t["district"]}' if t["district"] else f'{t["state"]} (at large)')
    return {"id": l["id"]["bioguide"], "kind": "fed", "name": l["name"].get("official_full") or f'{l["name"]["first"]} {l["name"]["last"]}',
            "party": t.get("party", ""), "office": chamber, "where": where,
            "photo": fed_photo(l["id"]["bioguide"])}


def federal_for(geo):
    st = geo["state"]
    out = []
    reps = [l for l in LEGS if l["terms"][-1]["type"] == "rep" and l["terms"][-1]["state"] == st]
    code = int(geo["cd_geoid"][-2:]) if geo.get("cd_geoid") else None
    for l in reps:
        d = l["terms"][-1]["district"]
        if len(reps) == 1 or (code is not None and (d == code or (d == 0 and code in (0, 98, 99)))):
            out.append(l)
    out.sort(key=lambda l: 0)
    sens = [l for l in LEGS if l["terms"][-1]["type"] == "sen" and l["terms"][-1]["state"] == st]
    return [fed_summary(l) for l in out + sens]


# ---------- state / local ----------
def state_summary(p, role):
    chamber = {"upper": "State Senator", "lower": "State Representative", "mayor": "Mayor"}.get(role["type"], role["type"])
    return {"id": p["id"], "kind": "st", "name": p["name"], "party": p["party"], "office": chamber,
            "where": (f'{p["state"]} district {role["district"]}' if role["district"] else (role["place"] or "").replace("_", " ").title()),
            "photo": p.get("image")}


def state_for_keyless(geo):
    """Match Census district names to Open States district names (works for numbered districts;
    some states use names like '1st Suffolk' that won't match)."""
    st = geo["state"]
    found, missed = [], []
    for kind, label in (("upper", "sldu"), ("lower", "sldl")):
        want = geo.get(label)
        if not want:
            continue
        hits = [(p, r) for p in STATE_PEOPLE if p["state"] == st for r in p["roles"]
                if r["type"] == kind and norm(r["district"]) == norm(want)]
        if not hits:
            missed.append(f'{kind} district "{want}"')
        found += [state_summary(p, r) for p, r in hits]
    return found, missed


def state_for_api(geo):
    r = requests.get("https://v3.openstates.org/people.geo",
                     params={"lat": geo["lat"], "lng": geo["lng"]},
                     headers={"X-API-KEY": OPENSTATES_KEY, **UA}, timeout=25)
    r.raise_for_status()
    out = []
    for person in r.json().get("results", []):
        role = person.get("current_role") or {}
        if role.get("org_classification") not in ("upper", "lower"):
            continue  # federal members come from congress-legislators
        pid = person["id"].replace("ocd-person/", "")
        local = STATE_BY_ID.get(pid)
        out.append({"id": pid, "kind": "st", "name": person["name"],
                    "party": person.get("party", ""),
                    "office": "State Senator" if role["org_classification"] == "upper" else "State Representative",
                    "where": f'{geo["state"]} district {role.get("district", "")}',
                    "photo": person.get("image") or (local or {}).get("image")})
    return out


def local_mayors(geo):
    if not geo.get("place"):
        return []
    slug = place_slug(geo["place"])
    out = []
    for p in STATE_PEOPLE:
        if p["state"] != geo["state"]:
            continue
        for r in p["roles"]:
            if r["type"] == "mayor" and r["place"] == slug:
                out.append(state_summary(p, r))
    return out


# ---------- routes ----------
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/lookup")
def lookup():
    address = (request.args.get("address") or "").strip()
    if not address:
        return render_template("home.html")
    notes = []
    try:
        geo = census_geocode(address)
    except Exception as e:
        return render_template("results.html", address=address, geo=None, error=f"Census geocoder error: {e}"), 502
    if not geo:
        return render_template("results.html", address=address, geo=None,
                               error="The Census geocoder could not match that address. Try adding city, state and ZIP."), 200
    federal = federal_for(geo)
    state, local = [], local_mayors(geo)
    if OPENSTATES_KEY:
        try:
            state = state_for_api(geo)
        except Exception as e:
            notes.append(f"Open States API failed ({e}); fell back to local data.")
    if not state:
        state, missed = state_for_keyless(geo)
        if missed and geo["state"] == "DC":
            notes.append("D.C. Council members are not covered yet.")
        elif missed:
            notes.append("We couldn't match every state legislative district for this address; some state legislators may be missing.")
    if not local:
        notes.append("Local officials (city council, county, school board) are not covered by a free source; only some mayors are listed.")
    return render_template("results.html", address=address, geo=geo, federal=federal, state=state,
                           local=local, notes=notes, error=None,
                           state_svg=state_svg(geo["state"]), flag_url=flag_url(geo["state"]))


@lru_cache(maxsize=256)
def sponsored_bills(bioguide):
    if not CONGRESS_KEY:
        return None
    r = requests.get(f"https://api.congress.gov/v3/member/{bioguide}/sponsored-legislation",
                     params={"api_key": CONGRESS_KEY, "format": "json", "limit": 10},
                     headers=UA, timeout=25)
    r.raise_for_status()
    out = []
    for b in r.json().get("sponsoredLegislation", []):
        out.append({"title": b.get("title"), "number": f'{b.get("type", "")} {b.get("number", "")}',
                    "congress": b.get("congress"), "introduced": b.get("introducedDate"),
                    "action": (b.get("latestAction") or {}).get("text"),
                    "url": f'https://www.congress.gov/bill/{b.get("congress")}th-congress/'
                           f'{_chamber_path(b.get("type", ""))}/{b.get("number")}'})
    return out


def _chamber_path(t):
    t = t.upper()
    return {"HR": "house-bill", "S": "senate-bill", "HRES": "house-resolution", "SRES": "senate-resolution",
            "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
            "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution"}.get(t, "house-bill")


@app.route("/official/fed/<bioguide>")
def fed_profile(bioguide):
    l = FED_BY_ID.get(bioguide)
    if not l:
        abort(404)
    t = l["terms"][-1]
    s = fed_summary(l)
    soc = SOCIAL.get(bioguide, {})
    links = []
    if t.get("url"): links.append(("Official website", t["url"]))
    if t.get("contact_form"): links.append(("Contact form", t["contact_form"]))
    links.append(("Congress.gov profile", f"https://www.congress.gov/member/x/{bioguide}"))
    if l["id"].get("ballotpedia"): links.append(("Ballotpedia", "https://ballotpedia.org/" + l["id"]["ballotpedia"].replace(" ", "_")))
    if soc.get("twitter"): links.append(("X / Twitter", "https://x.com/" + soc["twitter"]))
    if soc.get("facebook"): links.append(("Facebook", "https://facebook.com/" + soc["facebook"]))
    if soc.get("youtube"): links.append(("YouTube", "https://youtube.com/" + soc["youtube"]))
    bills, bills_error = None, None
    try:
        bills = sponsored_bills(bioguide)
    except Exception as e:
        bills_error = str(e)
    contact = {"Phone": t.get("phone"), "Office": t.get("address")}
    vlist, verr, vnote = votes_mod.recent_votes(bioguide, t["type"])
    return render_template("profile.html", p=s, votes=vlist, votes_error=verr, votes_note=vnote, since=l["terms"][0]["start"][:4], term_end=t.get("end"),
                           contact=contact, links=links, bills=bills, bills_error=bills_error,
                           has_key=bool(CONGRESS_KEY), kind="fed")


@app.route("/official/st/<pid>")
def st_profile(pid):
    p = STATE_BY_ID.get(pid)
    if not p:
        abort(404)
    role = p["roles"][0]
    s = state_summary(p, role)
    links = [(f"Official page {i+1}" if i else "Official page", u) for i, u in enumerate(p["links"][:3])]
    if p.get("ballotpedia"): links.append(("Ballotpedia", p["ballotpedia"]))
    contact = {"Email": p.get("email")}
    for o in p["offices"][:3]:
        contact[f'{(o.get("classification") or "office").title()} office'] = ", ".join(x for x in (o.get("address"), o.get("voice")) if x)
    return render_template("profile.html", p=s, votes=None, votes_error=None, votes_note=None, since=None, term_end=None, contact=contact, links=links,
                           bills=None, bills_error=None, has_key=False, kind="st")


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))

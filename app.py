"""weelect.org prototype: address -> elected officials, plus profile pages.

Run:  .venv/bin/python app.py   (http://127.0.0.1:5000)
Optional env vars (see README): CONGRESS_API_KEY, OPENSTATES_API_KEY
"""
import json, os, re, time
import votes as votes_mod
import elections as elections_mod
import bothsides as bs_mod
import measures as ms_mod
import getinvolved as gi_mod
import results as rs_mod
import localdata
from functools import lru_cache
import requests
import datetime
from markupsafe import Markup
from flask import Flask, render_template, request, abort, Response, url_for

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CONGRESS_KEY = os.environ.get("CONGRESS_API_KEY", "").strip()
OPENSTATES_KEY = os.environ.get("OPENSTATES_API_KEY", "").strip()
# "ACS2025_Current" = 119th Congress districts (who is seated today).
# "Current_Current" already returns the 120th Congress map (starts Jan 2027), which is wrong for sitting members.
CENSUS_VINTAGE = os.environ.get("CENSUS_VINTAGE", "ACS2025_Current")
UA = {"User-Agent": "weelect-prototype/0.1"}

app = Flask(__name__)


# Public base URL used for canonical links, Open Graph tags and the sitemap. Override with SITE_URL
# (for example when testing on a staging host) so canonicals never point at the wrong site.
SITE_URL = os.environ.get("SITE_URL", "https://weelect.org").rstrip("/")
# TODO(Bob): set these in Render -> Environment (or hard-code them here) before merging.
# Until they are set, the About page shows a clearly marked [TODO] instead of an address / name.
CORRECTIONS_EMAIL = os.environ.get("CORRECTIONS_EMAIL", "").strip()
RESPONSIBLE_NAME = os.environ.get("RESPONSIBLE_NAME", "").strip()
SITEMAP_INCLUDE_STATE = os.environ.get("SITEMAP_INCLUDE_STATE", "").strip() == "1"
OG_IMAGE_PATH = "/static/og-default.png"


@app.context_processor
def _globals():
    # The masthead date uses the same U.S. Eastern clock as the election countdown (elections._today),
    # so the two can never disagree in the evening when UTC is already "tomorrow".
    return {"today": elections_mod._today().strftime("%A, %B %-d, %Y"),
            "site_url": SITE_URL,
            "canonical_url": SITE_URL + request.path,
            "og_image": SITE_URL + OG_IMAGE_PATH,
            "corrections_email": CORRECTIONS_EMAIL,
            "responsible_name": RESPONSIBLE_NAME,
            "state_codes": STATE_CODES}


@app.after_request
def _security_headers(resp):
    # Cross-origin links and requests (official sites, OpenStreetMap tiles) get only our origin, never a path.
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Not a full script policy (the site uses a few small inline styles/scripts), but blocks framing, plugin
    # content, <base> hijacking and form posts to other sites.
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
    if request.path.startswith("/static/") and resp.status_code == 200:
        # CSS/JS are versioned with ?v=N in the templates; bump N when they change.
        resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# ---------- abuse protection ----------
MAX_ADDRESS_LEN = 200
_HITS = {}   # (bucket, ip) -> recent request times; per worker process, which is plenty for a small site


def _client_ip():
    return (request.headers.get("CF-Connecting-IP") or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "?")


def _rate_limited(bucket, limit, window=60):
    """True when this client has made more than `limit` requests to `bucket` in the last `window` seconds."""
    now = time.time()
    if len(_HITS) > 5000:   # keep memory bounded
        for k in [k for k, v in _HITS.items() if not v or now - v[-1] > window]:
            _HITS.pop(k, None)
    key = (bucket, _client_ip())
    q = [t for t in _HITS.get(key, []) if now - t < window]
    q.append(now)
    _HITS[key] = q
    return len(q) > limit


def _error_page(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(404)
def _not_found(e):
    return _error_page(404, "Page not found", "We couldn\u2019t find that page. It may have moved, or the address may have a typo.")


@app.errorhandler(405)
def _bad_method(e):
    return _error_page(405, "That didn\u2019t work", "Please go back to the home page and try again.")


@app.errorhandler(500)
def _server_error(e):
    return _error_page(500, "Something went wrong", "That was our mistake, not yours. Please try again in a minute.")


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
    return render_template("credits.html", flags=flags, bs_sources=bs_mod.all_sources(), ms_sources=ms_mod.all_sources(), gi_sources=list(gi_mod.SOURCES.values()))


@app.context_processor
def _bothsides_helpers():
    return {"bs_numbers": bs_mod.source_numbers, "bs_sourcelist": bs_mod.sources, "bs_roll_url": bs_mod.roll_url,
            "bs_for_vote": bs_mod.match_recent, "bs_meta": bs_mod.meta(),
            "ms_numbers": ms_mod.source_numbers, "ms_sourcelist": ms_mod.sources, "ms_match": ms_mod.match,
            "ms_meta": ms_mod.meta(), "ms_for_state": ms_mod.for_state,
            "rs_box": rs_mod.box, "rs_outcome": rs_mod.outcome, "rs_elect_note": rs_mod.elect_note}


@app.route("/votes")
def votes_index():
    return render_template("votes_index.html", items=bs_mod.items())


@app.route("/votes/<slug>")
def votes_item(slug):
    it = bs_mod.get(slug)
    if not it:
        abort(404)
    items = bs_mod.items()
    i = items.index(it)
    return render_template("votes_item.html", it=it, prev=items[i - 1] if i else None, nxt=items[i + 1] if i + 1 < len(items) else None)


@app.route("/get-involved")
def get_involved():
    order, ways = [], []
    for w in gi_mod.WAYS:
        for sid in w["src"]:
            if sid not in order:
                order.append(sid)
        ways.append(dict(w, nums=[order.index(sid) + 1 for sid in w["src"]]))
    # Sources cited in the "Official places to start" list are all in SOURCES; list every source in citation order.
    rest = [k for k in gi_mod.SOURCES if k not in order]
    sources = [(i + 1, gi_mod.SOURCES[k]) for i, k in enumerate(order + rest)]
    return render_template("get_involved.html", ways=ways, sources=sources, gi_checked=gi_mod.CHECKED)


@app.route("/measures")
def measures_index():
    return render_template("measures_index.html", groups=ms_mod.by_state(), total=len(ms_mod.items()),
                           has_recap=not _recap()["empty"])


@app.route("/measures/<slug>")
def measures_item(slug):
    it = ms_mod.get(slug)
    if not it:
        abort(404)
    items = ms_mod.items()
    i = items.index(it)
    return render_template("measures_item.html", it=it, prev=items[i - 1] if i else None, nxt=items[i + 1] if i + 1 < len(items) else None)


def _current_name(kind, oid):
    if kind == "fed" and oid in FED_BY_ID:
        return fed_summary(FED_BY_ID[oid])["name"]
    if kind == "st" and oid in STATE_BY_ID:
        return STATE_BY_ID[oid]["name"]
    return None


def _recap():
    return rs_mod.recap(ms_mod.items(), _current_name)


@app.route("/results")
def results_recap():
    """Post-election recap from certified, sourced data only. A 404 (and unlinked) until that data exists."""
    r = _recap()
    if r["empty"]:
        abort(404)
    return render_template("results_recap.html", r=r)


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
    if r.status_code == 400:   # the Census service rejects input it cannot parse at all: same as "no match"
        return None
    r.raise_for_status()
    try:
        matches = r.json()["result"]["addressMatches"]
    except ValueError:   # the Census firewall answers some odd input with an HTML "Request Rejected" page
        return None
    if not matches:
        return None
    m = matches[0]
    g = m["geographies"]

    def first(prefix_contains, exclude=None):
        for k, v in g.items():
            if prefix_contains in k and v:
                return v[0]
        return None

    def all_of(*names):
        out = []
        for n in names:
            for x in g.get(n) or []:
                out.append({"geoid": x.get("GEOID"), "name": x.get("NAME"), "layer": n})
        return out

    place = (g.get("Incorporated Places") or [{}])[0]
    cdp = (g.get("Census Designated Places") or [{}])[0]
    cousub = (g.get("County Subdivisions") or [{}])[0]
    return {
        "place_geoid": place.get("GEOID"), "cdp_geoid": cdp.get("GEOID"), "cdp_name": cdp.get("NAME"),
        "county_geoid": (g.get("Counties") or [{}])[0].get("GEOID"),
        "cousub_geoid": cousub.get("GEOID"), "cousub_name": cousub.get("NAME"),
        "school_districts": all_of("Unified School Districts", "Elementary School Districts", "Secondary School Districts"),
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
def party_class(party):
    """'R' / 'D' / 'N' (neutral: independents, other parties, nonpartisan, unknown) for badge colours."""
    p = (party or "").strip().lower()
    if p.startswith("republican"):
        return "R"
    if p.startswith("democrat"):
        return "D"
    return "N"


def initials(name):
    parts = [w for w in re.sub(r"[^A-Za-z .'-]", "", name or "").split() if w.rstrip(".").lower() not in ("jr", "sr", "ii", "iii", "iv", "dr", "mr", "mrs", "ms")]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def party_label(party):
    return party.strip() if party and party.strip() else "No party listed"


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
            "initials": initials(l["name"].get("official_full") or l["name"]["last"]),
            "party": party_label(t.get("party", "")), "pclass": party_class(t.get("party", "")),
            "office": chamber, "where": where,
            "photo": fed_photo(l["id"]["bioguide"])}


def federal_raw(geo):
    """congress-legislators records for this address: its House member(s), then its senators."""
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
    return out + sens


def federal_for(geo):
    return [fed_summary(l) for l in federal_raw(geo)]


# ---------- state / local ----------
def _https(u):
    """Official photo hosts serve https; an http:// image on our https pages would be blocked as mixed content."""
    return ("https://" + u[7:]) if isinstance(u, str) and u.startswith("http://") else u


def state_summary(p, role):
    chamber = {"upper": "State Senator", "lower": "State Representative", "mayor": "Mayor"}.get(role["type"], role["type"])
    return {"id": p["id"], "kind": "st", "name": p["name"], "initials": initials(p["name"]), "party": party_label(p["party"]),
            "pclass": party_class(p["party"]), "office": chamber,
            "where": (f'{p["state"]} district {role["district"]}' if role["district"] else (role["place"] or "").replace("_", " ").title()),
            "photo": _https(p.get("image"))}


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
        out.append({"id": pid, "kind": "st", "name": person["name"], "initials": initials(person["name"]),
                    "party": party_label(person.get("party", "")), "pclass": party_class(person.get("party", "")),
                    "office": "State Senator" if role["org_classification"] == "upper" else "State Representative",
                    "where": f'{geo["state"]} district {role.get("district", "")}',
                    "photo": _https(person.get("image") or (local or {}).get("image"))})
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


STATE_CODES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
               "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI").split()


def _private(resp):
    """Pages that contain or react to a home address: never cached, never indexed."""
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.route("/lookup", methods=["GET", "POST"])
def lookup():
    # POST keeps the address out of the URL (and so out of access logs, browser history, shared links and
    # Referer headers). A GET with ?address= is only a bridge for old links: it hands the address to an
    # auto-submitting POST form and renders nothing else.
    if request.method == "GET":
        legacy = (request.args.get("address") or "").strip()
        if not legacy:
            return _private(app.make_response(render_template("home.html")))
        return _private(app.make_response(render_template("lookup_bridge.html", address=legacy)))
    if _rate_limited("lookup", 20):
        return _private(app.make_response(_error_page(429, "Too many lookups", "Please wait a minute and try again.")))
    return _private(app.make_response(_lookup_post((request.form.get("address") or "").strip())))


@app.route("/city-interest", methods=["POST"])
def city_interest():
    """'Tell us your city': city + state only, never a street address. Nothing is stored in a database;
    one line is written to the application log (Render -> Logs) so Bob can see demand.
    TODO(Bob): replace the log line with a durable destination (email service or a form tool) if you want to keep these."""
    if _rate_limited("city", 6):
        return _private(app.make_response(_error_page(429, "Too many requests", "Please wait a minute and try again.")))
    city = " ".join((request.form.get("city") or "").split())
    st = (request.form.get("state") or "").strip().upper()
    ok = (not request.form.get("website")            # honeypot: real visitors leave it empty
          and 2 <= len(city) <= 60 and re.fullmatch(r"[A-Za-z .'\-]+", city) and st in STATE_CODES)
    if ok:
        print("CITY_INTEREST " + json.dumps({"city": city, "state": st}), flush=True)
    return _private(app.make_response((render_template("city_interest.html", ok=bool(ok), city=city, state=st), 200 if ok else 400)))


def _lookup_post(address):
    if not address:
        return render_template("home.html")
    address = " ".join(address.split())   # collapse newlines, tabs and runs of spaces
    if len(address) > MAX_ADDRESS_LEN:
        return render_template("results.html", address=address[:MAX_ADDRESS_LEN], geo=None,
                               error="That address is too long. Please enter just a street address, city and state."), 400
    if re.search(r"[<>{}\[\]|\\;=`^~$%*@!?]", address):
        return render_template("results.html", address="", geo=None,
                               error="Please enter a street address using only letters, numbers and ordinary punctuation, for example 1600 Pennsylvania Ave NW, Washington, DC."), 400
    if re.match(r"^\s*(p\.?\s*o\.?\s*box|post office box)\b", address, re.I):
        return render_template("results.html", address=address, geo=None,
                               error="P.O. boxes can\u2019t be matched to a voting district. Please enter the street address where you live."), 200
    notes = []
    try:
        geo = census_geocode(address)
    except Exception as e:
        app.logger.warning("census geocoder failed: %s", type(e).__name__)
        return render_template("results.html", address=address, geo=None,
                               error="The U.S. Census address service is not responding right now. Your address was not saved. Please try again in a few minutes."), 503
    if not geo:
        return render_template("results.html", address=address, geo=None,
                               error="The Census geocoder could not match that address. Try adding city, state and ZIP."), 200
    federal = federal_for(geo)
    state = []
    local = localdata.for_geo(geo)
    if OPENSTATES_KEY:
        try:
            state = state_for_api(geo)
        except Exception as e:
            app.logger.warning("open states failed: %s", type(e).__name__)
            notes.append("The live state-legislator lookup was unavailable, so we used our saved data instead.")
    if not state:
        state, missed = state_for_keyless(geo)
        if missed and geo["state"] == "DC":
            notes.append("D.C. Council members are not covered yet.")
        elif missed:
            notes.append("We couldn't match every state legislative district for this address; some state legislators may be missing.")
    try:
        elections = elections_mod.build_panel(geo, federal_raw(geo), address)
    except Exception as e:   # the elections panel must never break the officials page
        app.logger.exception("elections panel failed: %s", e)
        elections = None
    return render_template("results.html", address=address, geo=geo, federal=federal, state=state,
                           local=local, notes=notes, error=None, elections=elections,
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
    if soc.get("youtube"):
        yt = soc["youtube"]
        links.append(("YouTube", "https://www.youtube.com/" + ("channel/" + yt if re.fullmatch(r"UC[\w-]{22}", yt) else yt)))
    bills, bills_error = None, None
    try:
        bills = sponsored_bills(bioguide)
    except Exception as e:
        app.logger.warning("congress.gov sponsored bills failed: %s", type(e).__name__)
        bills_error = "temporarily unavailable"
    contact = {"Phone": t.get("phone"), "Office": t.get("address")}
    vlist, verr, vnote = votes_mod.recent_votes(bioguide, t["type"])
    try:
        notable = bs_mod.member_votes(bioguide, t["type"])
    except Exception:
        notable = []
    return render_template("profile.html", p=s, notable=notable, votes=vlist, votes_error=verr, votes_note=vnote, since=l["terms"][0]["start"][:4], term_end=t.get("end"),
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
    return render_template("profile.html", p=s, notable=[], votes=None, votes_error=None, votes_note=None, since=None, term_end=None, contact=contact, links=links,
                           bills=None, bills_error=None, has_key=False, kind="st")


@app.route("/BingSiteAuth.xml")
def bing_site_auth():
    body = ('<?xml version="1.0"?>\n<users>\n\t<user>2FBEF01D24EF9871F1D7D5A4943756F7</user>\n</users>')
    return Response(body, mimetype="application/xml")


@app.route("/robots.txt")
def robots_txt():
    body = ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /lookup\n"
            "Disallow: /city-interest\n"
            f"\nSitemap: {SITE_URL}/sitemap.xml\n")
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    from xml.sax.saxutils import escape
    urls = [("/", None), ("/get-involved", gi_mod.CHECKED), ("/votes", bs_mod.meta().get("last_reviewed")), ("/measures", ms_mod.meta().get("last_reviewed")), ("/credits", None)]
    if not _recap()["empty"]:
        urls.append(("/results", None))
    urls += [(f"/votes/{it['id']}", bs_mod.meta().get("last_reviewed")) for it in bs_mod.items()]
    urls += [(f"/measures/{it['id']}", ms_mod.meta().get("last_reviewed")) for it in ms_mod.items()]
    urls += [(f"/official/fed/{b}", None) for b in sorted(FED_BY_ID)]
    if SITEMAP_INCLUDE_STATE:   # ~7,700 thinner pages; off by default (see PR notes)
        urls += [(f"/official/st/{pid}", None) for pid in sorted(STATE_BY_ID)]
    rows = "".join(f"<url><loc>{escape(SITE_URL + path)}</loc>" + (f"<lastmod>{escape(str(last))}</lastmod>" if last else "") + "</url>"
                   for path, last in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + rows + "</urlset>\n")
    return Response(xml, mimetype="application/xml")


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))

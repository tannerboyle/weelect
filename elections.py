"""Elections panel: next election date, offices up, ballot measures, polling places and hours.

Everything shown carries a source label. Nothing is guessed:
  * Election date      - federal law (2 U.S.C. 7): Tuesday after the first Monday in November.
  * U.S. House / Senate- derived from congress-legislators term end dates (data/legislators-current.json).
  * Governor, legislature, statewide measures, hours, early voting, deadlines
                       - data/elections_2026.json (built by scripts/fetch_elections.py from Ballotpedia and
                         Wikipedia, each row keeps its source URL).
  * Exact polling place / early vote sites / drop boxes / local contests and measures
                       - Google Civic Information API (voterinfo) ONLY when GOOGLE_CIVIC_API_KEY is set.
                         Without a key the panel links to the state's official lookup instead.
"""
import datetime, json, os, re, threading, time
from functools import lru_cache
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CIVIC_KEY = os.environ.get("GOOGLE_CIVIC_API_KEY", "").strip()
CIVIC_ELECTION_ID = os.environ.get("GOOGLE_CIVIC_ELECTION_ID", "12000").strip()  # VIP: Nov. 3, 2026 General
UA = {"User-Agent": "weelect-prototype/0.1"}

BP_DATES = "https://ballotpedia.org/2026_election_and_voting_dates"
VOTE_GOV = "https://vote.gov/"
NASS = "https://www.nass.org/can-I-vote"
STATUTE = "https://www.law.cornell.edu/uscode/text/2/7"
HOUSE_END = "2027-01-03"     # terms of all House members and the Class II senators end here


@lru_cache(maxsize=1)
def _data():
    with open(os.path.join(ROOT, "data", "elections_2026.json")) as f:
        return json.load(f)


def election_day(year=2026):
    d = datetime.date(year, 11, 1)
    while d.weekday() != 0:          # first Monday
        d += datetime.timedelta(days=1)
    return d + datetime.timedelta(days=1)


def _today():
    # Election logic follows U.S. time; the server clock is UTC.
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.date.today()


_MON = {m: i for i, m in enumerate(["Jan", "Feb", "March", "April", "May", "June", "July", "Aug", "Sept", "Oct", "Nov", "Dec"], 1)}


def _dates_in(s):
    out = []
    for m in re.finditer(r"([A-Z][a-z]+)\.? (\d{1,2}), (\d{4})", s or ""):
        mon = _MON.get(m.group(1)) or _MON.get(m.group(1)[:3]) or _MON.get(m.group(1)[:4])
        if mon:
            out.append(datetime.date(int(m.group(3)), mon, int(m.group(2))))
    return out


def _nice(d):
    return d.strftime("%b. %-d").replace("May.", "May")


def _short(v):
    return (v or "").replace(", 2026", "")


def _join_values(vals):
    """Turn Ballotpedia's per-method columns (in-person / online / mail) into one readable line."""
    groups = {}
    for k, v in vals.items():
        if k.lower().endswith("type") or not v:
            continue
        method = k.split()[0].lower()
        label = {"in-person": "in person", "online": "online", "mail": "by mail"}.get(method, method)
        typ = vals.get(k + " type") if method == "mail" else None
        if typ:
            label += f" ({typ.lower()})"
        groups.setdefault(_short(v), []).append(label)
    if len(groups) == 1:
        v, labels = next(iter(groups.items()))
        if v.lower().startswith("all"):
            return "All-mail election (no request needed)" if "request" in " ".join(vals) else "All-mail election"
        return f"{v} — {'; '.join(labels)}"
    return "; ".join(f"{v} — {', '.join(labels)}" for v, labels in groups.items())


def _status(dates, today, kind="deadline"):
    if not dates:
        return None
    if kind == "window":
        a, b = min(dates), max(dates)
        if today < a:
            return ("upcoming", f"Opens {_nice(a)}")
        if today <= b:
            return ("open", "Open now")
        return ("past", "Ended")
    last = max(dates)
    if last < today:
        return ("past", "Passed")
    if min(dates) <= today:
        return ("open", "Still open for some methods")
    return ("upcoming", "Upcoming")


# ---------------- federal: derived from term end dates ----------------
def _fed_up(raw, st, today):
    out = []
    iso = today.isoformat()
    for l in raw:
        t = l["terms"][-1]
        name = l["name"].get("official_full") or f'{l["name"]["first"]} {l["name"]["last"]}'
        party = {"Republican": "R", "Democrat": "D"}.get(t.get("party"), (t.get("party") or "")[:1])
        who = f"{name} ({party})" if party else name
        if t["type"] == "rep" and HOUSE_END >= t["end"] >= iso:
            seat = (f'{st} at large' if t["district"] == 0 else f'{st}-{t["district"]}')
            if st in ("DC", "GU", "VI", "AS", "MP"):
                title, detail = "U.S. House — Delegate", f"All House and delegate seats are on the ballot every two years. Seat now held by {who}."
            else:
                title, detail = f"U.S. House — {seat}", f"All 435 House seats are on the ballot every two years. Seat now held by {who}."
            race = f'https://www.fec.gov/data/elections/house/{st}/{t["district"]:02d}/2026/'
            out.append({"level": "Federal", "title": title, "detail": detail, "link": race, "link_label": "Race filings on FEC.gov"})
        if t["type"] == "sen" and t["end"] <= HOUSE_END and t["end"] >= iso:
            special = t["end"] < HOUSE_END
            detail = (f"Special election to finish a Senate term; seat now held by {who}, who was appointed." if special
                      else f"Regular six-year term (Class II). Seat now held by {who}.")
            out.append({"level": "Federal", "title": "U.S. Senate" + (" — special election" if special else ""),
                        "detail": detail, "link": f"https://www.fec.gov/data/elections/senate/{st}/2026/",
                        "link_label": "Race filings on FEC.gov"})
    return out


# ---------------- Google Civic (optional key) ----------------
_cache = {}


def _addr(a):
    a = a or {}
    line = ", ".join(x for x in [a.get("line1"), a.get("line2"), a.get("line3")] if x)
    city = ", ".join(x for x in [a.get("city"), (a.get("state") or "") + (" " + a["zip"] if a.get("zip") else "")] if x and x.strip())
    return {"place": a.get("locationName") or "", "address": ", ".join(x for x in [line, city] if x)}


def _num(v):
    try:
        f = float(v)
        return f if -180 <= f <= 180 else None
    except (TypeError, ValueError):
        return None


_HRS = re.compile(r"^\s*(?P<day>[A-Za-z]{3},? [A-Za-z]{3,9}\.? \d{1,2}):\s*(?P<t>.+?)\s*$")


def compact_hours(h):
    """'Mon, Oct 19: 7 am - 7 pm' x12 lines -> 'Mon, Oct 19 – Fri, Oct 30: 7 am - 7 pm' (consecutive identical times)."""
    lines = [l for l in (h or "").splitlines() if l.strip()]
    parsed = [_HRS.match(l) for l in lines]
    if len(lines) < 3 or not all(parsed):
        return (h or "").strip()
    runs = []
    for m in parsed:
        if runs and runs[-1][2] == m.group("t"):
            runs[-1][1] = m.group("day")
        else:
            runs.append([m.group("day"), m.group("day"), m.group("t")])
    if len(runs) > 6:
        return (h or "").strip()
    return "\n".join(f"{a}: {t}" if a == b else f"{a} – {b}: {t}" for a, b, t in runs)


def _site(s, kind=""):
    a = _addr(s.get("address"))
    o = any(x.get("official") for x in s.get("sources") or [])
    return {"kind": kind, "lat": _num(s.get("latitude")), "lng": _num(s.get("longitude")),
            "name": s.get("name") or a["place"], "place": a["place"], "address": a["address"],
            "hours": compact_hours(s.get("pollingHours") or ""), "notes": (s.get("notes") or "").strip(),
            "start": s.get("startDate") or "", "end": s.get("endDate") or "", "official": o,
            "map": "https://www.google.com/maps/search/?api=1&query=" + requests.utils.quote(a["address"]) if a["address"] else None}


def parse_civic(j):
    """Normalise a voterinfo response into what the template shows."""
    res = {"status": "ok", "election": (j.get("election") or {}).get("name"), "day": (j.get("election") or {}).get("electionDay"),
           "polling": [_site(s, "polling") for s in j.get("pollingLocations") or []],
           "early": [_site(s, "early") for s in j.get("earlyVoteSites") or []],
           "dropoff": [_site(s, "dropoff") for s in j.get("dropOffLocations") or []],
           "mail_only": bool(j.get("mailOnly")), "contests": [], "measures": [], "admin": {}}
    for c in j.get("contests") or []:
        official = any(x.get("official") for x in c.get("sources") or [])
        if c.get("referendumTitle") or c.get("type") == "Referendum":
            res["measures"].append({"title": c.get("referendumTitle") or c.get("ballotTitle") or "Ballot measure",
                                    "summary": c.get("referendumSubtitle") or c.get("referendumBrief") or "",
                                    "url": c.get("referendumUrl"), "scope": (c.get("district") or {}).get("name") or "",
                                    "official": official})
        else:
            res["contests"].append({"office": c.get("office") or c.get("ballotTitle") or "Contest",
                                    "scope": (c.get("district") or {}).get("name") or "", "official": official,
                                    "vote_for": c.get("numberVotingFor"),
                                    "candidates": [{"name": x.get("name"), "party": x.get("party") or "", "url": x.get("candidateUrl")}
                                                   for x in sorted(c.get("candidates") or [], key=lambda x: x.get("orderOnBallot") or 0)]})
    for s in j.get("state") or []:
        body = s.get("electionAdministrationBody") or {}
        loc = (s.get("local_jurisdiction") or {}).get("electionAdministrationBody") or {}
        res["admin"] = {"state_name": body.get("name"), "info": body.get("electionInfoUrl"), "finder": body.get("votingLocationFinderUrl"),
                        "absentee": body.get("absenteeVotingInfoUrl"), "ballot": body.get("ballotInfoUrl"),
                        "local_name": loc.get("name"), "local_finder": loc.get("votingLocationFinderUrl"), "local_info": loc.get("electionInfoUrl")}
        break
    return res


def civic_lookup(address):
    """None when no key is configured. {'status': 'none'|'error'|'ok', ...} otherwise. Cached 15 minutes."""
    if not CIVIC_KEY:
        return None
    hit = _cache.get(address)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    try:
        r = requests.get("https://www.googleapis.com/civicinfo/v2/voterinfo",
                         params={"address": address, "electionId": CIVIC_ELECTION_ID, "key": CIVIC_KEY, "returnAllAvailableData": "true"},
                         headers=UA, timeout=20)
        if r.status_code in (400, 404):
            out = {"status": "none"}      # no data published yet for this address / address not found
        elif r.status_code != 200:
            out = {"status": "error", "code": r.status_code}
        else:
            out = parse_civic(r.json())
            if not (out["polling"] or out["early"] or out["dropoff"] or out["contests"] or out["measures"]):
                out["status"] = "none"
    except Exception:
        out = {"status": "error", "code": 0}
    _cache[address] = (time.time(), out)
    return out



# ---------------- map data (voter pin + voting-site pins) ----------------
GEO_CACHE_PATH = os.path.join(ROOT, "cache", "site_geocode.json")
_geo_mem = None
_geo_lock = threading.Lock()
MAX_SITES = 60            # markers sent to the browser per request
MAX_GEOCODE = 12          # Census lookups per request for sites Google gave no coordinates for
FIT_NEAREST = 12          # the map zooms to the voter plus this many nearest sites
KINDS = {"polling": ("P", "Election Day polling place"), "early": ("E", "Early voting site"), "dropoff": ("D", "Ballot drop-off")}


def _geo_load():
    global _geo_mem
    if _geo_mem is None:
        try:
            with open(GEO_CACHE_PATH) as f:
                _geo_mem = json.load(f)
        except Exception:
            _geo_mem = {}
    return _geo_mem


def _geo_save():
    try:
        os.makedirs(os.path.dirname(GEO_CACHE_PATH), exist_ok=True)
        with open(GEO_CACHE_PATH + ".tmp", "w") as f:
            json.dump(_geo_mem, f)
        os.replace(GEO_CACHE_PATH + ".tmp", GEO_CACHE_PATH)
    except Exception:
        pass


def census_site_geocode(address):
    """Free U.S. Census geocoder, one address. Result cached in memory + cache/site_geocode.json
    (misses are remembered for a day so a bad address is not retried on every page view)."""
    key = re.sub(r"\s+", " ", address.strip().lower())
    cache = _geo_load()
    hit = cache.get(key)
    if hit and (hit.get("lat") is not None or time.time() - hit.get("ts", 0) < 86400):
        return (hit["lat"], hit["lng"]) if hit.get("lat") is not None else None
    try:
        r = requests.get("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                         params={"address": address, "benchmark": "Public_AR_Current", "format": "json"}, headers=UA, timeout=8)
        m = r.json()["result"]["addressMatches"]
        val = {"lat": m[0]["coordinates"]["y"], "lng": m[0]["coordinates"]["x"], "ts": time.time()} if m else {"lat": None, "lng": None, "ts": time.time()}
    except Exception:
        return None              # transient error: do not cache
    with _geo_lock:
        cache[key] = val
    return (val["lat"], val["lng"]) if val["lat"] is not None else None


def _km(a, b):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 12742 * asin(sqrt(h))


def build_map(geo, civic):
    """Return the data the map needs, or None (-> the panel hides the map)."""
    if not civic or civic.get("status") != "ok":
        return None
    try:
        voter = (float(geo["lat"]), float(geo["lng"]))
    except (KeyError, TypeError, ValueError):
        return None
    sites = [s for k in ("polling", "early", "dropoff") for s in civic.get(k, [])]
    missing = [s for s in sites if s["lat"] is None and s["address"]]
    if missing:      # only for sites Google gave no coordinates for: cached ones are free, at most MAX_GEOCODE new lookups
        cache = _geo_load()
        norm = lambda a: re.sub(r"\s+", " ", a.strip().lower())
        fresh = [s for s in missing if norm(s["address"]) not in cache][:MAX_GEOCODE]
        if fresh:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(lambda x: census_site_geocode(x["address"]), fresh))
            _geo_save()
        for s in missing:
            hit = cache.get(norm(s["address"]))
            if hit and hit.get("lat") is not None:
                s["lat"], s["lng"] = hit["lat"], hit["lng"]
    merged = {}
    for s in sites:
        if s["lat"] is None or s["lng"] is None:
            continue        # could not be placed; still listed in the text
        key = (round(s["lat"], 4), round(s["lng"], 4))      # ~11 m: one pin per building, even if listed under two names
        m = merged.setdefault(key, {"lat": s["lat"], "lng": s["lng"], "name": s["place"] or s["name"] or "Voting location",
                                    "address": s["address"], "map": s["map"], "entries": []})
        m["entries"].append({"name": s["place"] or s["name"] or "", "kind": s["kind"], "label": KINDS[s["kind"]][1], "hours": s["hours"], "notes": s["notes"],
                             "dates": (s["start"] + (" – " + s["end"] if s["end"] else "")) if s["start"] else ""})
    pts = sorted(merged.values(), key=lambda m: _km(voter, (m["lat"], m["lng"])))[:MAX_SITES]
    if not pts:
        return None
    for m in pts:
        m["letter"] = KINDS[m["entries"][0]["kind"]][0]
        m["miles"] = round(_km(voter, (m["lat"], m["lng"])) * 0.621371, 1)
    return {"voter": {"lat": voter[0], "lng": voter[1], "label": geo.get("matched") or "Your address"},
            "sites": pts, "fit": FIT_NEAREST, "total": len(pts), "unplaced": len([s for s in sites if s["lat"] is None])}

# ---------------- the panel ----------------
def build_panel(geo, fed_raw, address):
    D = _data()
    st = geo["state"]
    S = D["states"].get(st)
    if not S:
        return None
    today = _today()
    eday = election_day()
    days = (eday - today).days
    when = ("Election Day is today" if days == 0 else f"{days} day{'s' if days != 1 else ''} away" if days > 0
            else "This election has been held")
    panel = {"name": S["name"], "date": eday.strftime("%A, %B %-d, %Y"), "days": days, "when": when,
             "past": days < 0, "retrieved": D["meta"]["retrieved"], "bp": S["ballotpedia"]}

    # key dates (state level)
    rows = []
    e = S["early"]
    ev = e["values"]
    if (ev.get("Early voting begins") or "").lower() == "none":
        rows.append({"label": "Early in-person voting", "value": "Not offered in this state" + (" (voting is largely by mail)" if "mail" in (S.get("hours_note") or "").lower() else ""), "status": None, "source": e["source"]})
    else:
        ds = _dates_in(" ".join(ev.values()))
        stt = _status(ds, today, "window")
        rows.append({"label": "Early in-person voting", "value": f'{_short(ev.get("Early voting begins", "?"))} – {_short(ev.get("Early voting ends", "?"))}',
                     "status": stt, "source": e["source"]})
    for key, label in (("register", "Voter registration deadline"), ("mail_request", "Mail-ballot request deadline"), ("mail_return", "Mail-ballot return deadline")):
        b = S[key]
        vals = b["values"]
        rows.append({"label": label, "value": _join_values(vals), "status": _status(_dates_in(" ".join(vals.values())), today), "source": b["source"]})
    rows.append({"label": "Election Day polls", "value": S["hours"] + (" (local time)" if re.search(r"\d", S["hours"]) else ""), "status": None, "source": S["hours_src"],
                 "note": S.get("hours_note") or ""})
    from urllib.parse import urlparse
    for r in rows:
        host = urlparse(r["source"]).netloc.replace("www.", "")
        r["source_label"] = "Ballotpedia" if "ballotpedia.org" in host else f"{host}, via Ballotpedia"
    panel["rows"] = rows

    # offices
    offices = _fed_up(fed_raw, st, today)
    senators = [l for l in fed_raw if l["terms"][-1]["type"] == "sen"]
    if st not in ("DC",) and senators and not any(o["title"].startswith("U.S. Senate") for o in offices):
        offices.append({"level": "Federal", "title": "U.S. Senate — no election this year", "detail": f"Neither of {S['name']}'s Senate seats is on the November 2026 ballot.", "link": None})
    if st == "DC":
        offices.append({"level": "Federal", "title": "U.S. Senate — none", "detail": "The District of Columbia has no U.S. senators.", "link": None})
    if S["governor"]:
        offices.append({"level": "State", "title": "Governor", "detail": f"{S['name']} elects its governor on Nov. 3, 2026.",
                        "link": D["meta"]["sources"]["wikipedia_governors"], "link_label": "Source: Wikipedia"})
    elif st == "DC":
        offices.append({"level": "Local", "title": "Mayor of the District of Columbia", "detail": "The D.C. mayoral election is on Nov. 3, 2026; the D.C. Council also has seats on the ballot.",
                        "link": D["meta"]["sources"]["wikipedia_governors"], "link_label": "Source: Wikipedia"})
    else:
        offices.append({"level": "State", "title": "Governor — no election this year", "detail": f"{S['name']} does not elect a governor in 2026.", "link": None})
    leg = S.get("legislature")
    if leg:
        wl = D["meta"]["sources"]["wikipedia_legislatures"]
        for key, label, dist in (("upper", "State Senate", geo.get("sldu")), ("lower", "State House", geo.get("sldl")), ("unicameral", "State Legislature (unicameral)", geo.get("sldu"))):
            b = leg.get(key)
            if not b:
                continue
            dlabel = (f"District {dist}" if str(dist).isdigit() else f"{dist} district") if dist else ""
            where = f" {dlabel}" if dlabel else ""
            if b["up"] == 0:
                t, d = f"{label} — no election this year", f"No {label.lower()} seats are up in {S['name']} in 2026."
            elif b["up"] == b["total"]:
                t, d = f"{label} —{where or ' your district'}", f"All {b['total']} seats are up, including yours{(' (' + dlabel + ')') if dist else ''}."
            else:
                t = f"{label} —{where or ' your district'}"
                d = (f"{b['up']} of {b['total']} seats are up statewide (staggered terms). Whether{where or ' your district'} is one of them depends on the district; "
                     f"see the Ballotpedia page for {S['name']}.")
            offices.append({"level": "State", "title": t, "detail": d, "link": wl, "link_label": "Source: Wikipedia"})
    panel["offices"] = offices

    # measures
    panel["measures"] = [dict(m, type_label=D["meta"]["measure_types"].get(m["type"], m["type"])) for m in S["measures"]]

    # polling
    panel["polling"] = {"lookup": S["lookup"], "hours": S["hours"], "hours_note": S.get("hours_note") or "", "vote_gov": VOTE_GOV, "nass": NASS}
    panel["civic"] = civic_lookup(address)
    try:
        panel["map"] = build_map(geo, panel["civic"])
    except Exception:      # a map problem must never break the panel
        panel["map"] = None
    panel["civic_enabled"] = bool(CIVIC_KEY)
    return panel

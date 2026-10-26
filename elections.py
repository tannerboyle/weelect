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
import datetime, json, os, re, time
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
    city = ", ".join(x for x in [a.get("city"), (a.get("state") or "") + (" " + a["zip"] if a.get("zip") else "")] if x.strip())
    return {"place": a.get("locationName") or "", "address": ", ".join(x for x in [line, city] if x)}


def _site(s):
    a = _addr(s.get("address"))
    o = any(x.get("official") for x in s.get("sources") or [])
    return {"name": s.get("name") or a["place"], "place": a["place"], "address": a["address"],
            "hours": (s.get("pollingHours") or "").strip(), "notes": (s.get("notes") or "").strip(),
            "start": s.get("startDate") or "", "end": s.get("endDate") or "", "official": o,
            "map": "https://www.google.com/maps/search/?api=1&query=" + requests.utils.quote(a["address"]) if a["address"] else None}


def parse_civic(j):
    """Normalise a voterinfo response into what the template shows."""
    res = {"status": "ok", "election": (j.get("election") or {}).get("name"), "day": (j.get("election") or {}).get("electionDay"),
           "polling": [_site(s) for s in j.get("pollingLocations") or []],
           "early": [_site(s) for s in j.get("earlyVoteSites") or []],
           "dropoff": [_site(s) for s in j.get("dropOffLocations") or []],
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
    panel["civic_enabled"] = bool(CIVIC_KEY)
    return panel

"""Post-election helpers: official results links, certified measure outcomes, officials-elect notes, /results recap.

We are not a newsroom: nothing here fetches or shows live counts. Phase 1 links each state's official results page
(data/official_results.json; every URL fetched on meta.checked). Outcome stamps and officials-elect notes render only
from certified data that carries a source URL; until that data is filled in, nothing is shown and /results is a 404.
"""
import datetime, json, os
import elections as elections_mod

ROOT = os.path.dirname(os.path.abspath(__file__))
LINKS_PATH = os.path.join(ROOT, "data", "official_results.json")
ELECT_PATH = os.path.join(ROOT, "data", "officials_elect.json")
ELECTION_DAY = datetime.date(2026, 11, 3)
_cache = {}

STATE_NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "the District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def _load(path):
    m = os.path.getmtime(path)
    hit = _cache.get(path)
    if not hit or hit[0] != m:
        with open(path) as f:
            hit = _cache[path] = (m, json.load(f))
    return hit[1]


def links():
    return _load(LINKS_PATH)


def today():
    """Same U.S. Eastern clock as the masthead and the election countdown."""
    return elections_mod._today()


def phase(day=None):
    """'before' Election Day, 'election_day', or 'after'."""
    day = day or today()
    if day < ELECTION_DAY:
        return "before"
    return "election_day" if day == ELECTION_DAY else "after"


def _possessive(name):
    return name + "\u2019" + ("" if name.endswith("s") else "s")


def box(state, day=None):
    """Everything the 'Official results' box needs for one state, or None if we have no checked link for it."""
    s = links()["states"].get((state or "").upper())
    if not s:
        return None
    name = STATE_NAMES.get(state.upper(), state)
    owner = _possessive(name)
    ph = phase(day)
    if ph == "before":
        text = f"Results will appear on {owner} official site after polls close on Nov. 3."
    elif ph == "election_day":
        text = f"Results will appear on {owner} official site as polls close and counties report tonight."
    else:
        text = f"Results are posted on {owner} official site as counties report them."
    return {"state": state.upper(), "state_name": name, "agency": s["agency"], "url": s["url"], "phase": ph, "text": text,
            "unofficial": "Counts are unofficial until the state certifies them.", "checked": links()["meta"]["checked"]}


_AP_MON = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]


def ap_date(d):
    """AP style, as used across the site: 'Nov. 3, 2026', 'March 2, 2027'."""
    return f"{_AP_MON[d.month - 1]} {d.day}, {d.year}"


# ---------- certified ballot-measure outcomes ----------
def _date(s):
    try:
        return datetime.date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 100 else None


def outcome(item):
    """A validated, certified outcome for a measure, or None. Needs outcome (passed/failed), certified_date and source_url;
    yes_pct / no_pct are shown when present and valid. Anything incomplete renders nothing (accuracy over coverage)."""
    if not item:
        return None
    o = (item.get("outcome") or "").strip().lower()
    cert = _date(item.get("certified_date"))
    src = (item.get("source_url") or "").strip()
    if o not in ("passed", "failed") or not cert or not src.startswith("https://"):
        return None
    yes, no = _pct(item.get("yes_pct")), _pct(item.get("no_pct"))
    return {"outcome": o, "label": "Passed" if o == "passed" else "Failed", "yes_pct": yes, "no_pct": no,
            "certified": cert, "certified_label": ap_date(cert), "source_url": src}


# ---------- officials elect ----------
_REQ = ("kind", "id", "seat", "elected_name", "takes_office", "certified_date", "source_url")


def elect_entries():
    out = []
    for e in _load(ELECT_PATH).get("entries", []):
        if not all(e.get(k) for k in _REQ) or e["kind"] not in ("fed", "st") or not str(e["source_url"]).startswith("https://"):
            continue
        start, cert = _date(e["takes_office"]), _date(e["certified_date"])
        if not start or not cert:
            continue
        out.append(dict(e, takes_office_label=ap_date(start), certified_label=ap_date(cert), started=today() >= start))
    return out


def elect_note(kind, oid, current_name=None):
    """Note for the official currently shown on a card/profile, or None. Hidden once the new term has started."""
    for e in elect_entries():
        if e["kind"] == kind and e["id"] == oid and not e["started"]:
            same = current_name and e["elected_name"].strip().lower() == current_name.strip().lower()
            text = (f"Re-elected Nov. 3, 2026; new term begins {e['takes_office_label']}." if same else
                    f"{e['elected_name']} was elected to this seat on Nov. 3, 2026 and takes office {e['takes_office_label']}.")
            return dict(e, text=text, reelected=bool(same))
    return None


# ---------- /results recap ----------
def recap(measure_items, name_for=None):
    """{'measures': [(state_name, [(item, outcome)])], 'seats': [entry], 'empty': bool}. Seats list only changes of officeholder."""
    by_state = {}
    for it in measure_items:
        o = outcome(it)
        if o:
            by_state.setdefault(it["state_name"], []).append((it, o))
    seats = []
    for e in elect_entries():
        cur = name_for(e["kind"], e["id"]) if name_for else None
        if not cur or cur.strip().lower() != e["elected_name"].strip().lower():
            seats.append(dict(e, outgoing=cur))
    measures = sorted(by_state.items())
    return {"measures": measures, "seats": seats, "empty": not measures and not seats,
            "n_passed": sum(o["outcome"] == "passed" for _, g in measures for _, o in g),
            "n_failed": sum(o["outcome"] == "failed" for _, g in measures for _, o in g)}

"""'Both sides' editorial content for a few notable roll-call votes.

Data lives in data/counterarguments.json (hand-written and source-checked; see README "Both sides").
This module loads it, numbers each item's sources for footnotes, and looks up how a given member voted on the
roll calls each item covers (House: Clerk XML, cached by votes.py; Senate: Voteview bulk CSV).
"""
import csv, json, os, re, threading, time
import votes as V

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "data", "counterarguments.json")
_lock = threading.Lock()
_data = {"mtime": 0, "doc": None}
_sen = {"t": 0, "casts": None, "icpsr": None}


def doc():
    m = os.path.getmtime(PATH)
    if _data["doc"] is None or m != _data["mtime"]:
        _data["doc"] = json.load(open(PATH))
        _data["mtime"] = m
    return _data["doc"]


def meta():
    return doc()["meta"]


def items():
    return doc()["items"]


def get(slug):
    return next((i for i in items() if i["id"] == slug), None)


def sources(item):
    """Ordered [(n, id, source)] for every source an item cites, numbered by first use."""
    S = doc()["sources"]
    order = []
    for sid in item.get("summary_src", []) + [s for side in ("for", "against", "unsaid") for p in item.get(side, []) for s in p["src"]]:
        if sid not in order:
            order.append(sid)
    return [(n + 1, sid, S[sid]) for n, sid in enumerate(order)]


def source_numbers(item):
    return {sid: n for n, sid, _ in sources(item)}


def all_sources():
    """Every source cited anywhere, grouped by item, for the credits page."""
    return [(it, sources(it)) for it in items()]


def roll_url(r):
    if r["chamber"] == "House":
        return f"https://clerk.house.gov/Votes/{r['year']}{r['roll']}"
    return f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote119{r['session']}/vote_119_{r['session']}_{r['roll']:05d}.htm"


def roll_key(source_url):
    """('House', year, roll) / ('Senate', session, roll) from the vote source links votes.py produces, else None."""
    m = re.search(r"clerk\.house\.gov/Votes/(\d{4})(\d+)$", source_url or "")
    if m:
        return ("House", int(m.group(1)), int(m.group(2)))
    m = re.search(r"vote_119_(\d)_(\d{5})\.xml", source_url or "")
    if m:
        return ("Senate", int(m.group(1)), int(m.group(2)))
    return None


def match_recent(v):
    """The both-sides item for a vote from votes.recent_votes(), if one exists."""
    k = roll_key(v.get("source"))
    if not k:
        return None
    for it in items():
        for r in it["rolls"]:
            if (r["chamber"], r.get("year") or r.get("session"), r["roll"]) == k:
                return it
    return None


def _senate_load():
    """icpsr by bioguide + cast codes for just the Senate roll calls we feature (one pass over Voteview votes.csv, cached 12 h)."""
    if _sen["casts"] is not None and time.time() - _sen["t"] < 12 * 3600:
        return
    want = {r["vv"] for it in items() for r in it["rolls"] if r["chamber"] == "Senate"}
    icpsr = {}
    for r in csv.DictReader(open(V._vv_file("members"))):
        if r["chamber"] == "Senate" and r["bioguide_id"]:
            icpsr[r["bioguide_id"]] = r["icpsr"]
    casts = {}
    for r in csv.DictReader(open(V._vv_file("votes"))):
        if r["chamber"] == "Senate" and r["congress"] == "119" and int(r["rollnumber"]) in want:
            casts[(r["icpsr"], int(r["rollnumber"]))] = int(float(r["cast_code"]))
    _sen.update(t=time.time(), casts=casts, icpsr=icpsr)


def _house_vote(bioguide, r):
    d = V._house_roll(r["year"], r["roll"])
    v = d["votes"].get(bioguide) if d else None
    return {"Aye": "Yea", "No": "Nay"}.get(v, v)


def _senate_vote(bioguide, r):
    with _lock:
        _senate_load()
    ic = _sen["icpsr"].get(bioguide)
    cc = _sen["casts"].get((ic, r["vv"])) if ic else None
    return None if cc is None else V.CAST.get(cc, str(cc))


def member_votes(bioguide, chamber_type):
    """[{item, roll, vote}] for each featured item where this member has a recorded vote in their chamber ('rep' or 'sen')."""
    chamber = "Senate" if chamber_type == "sen" else "House"
    out = []
    for it in items():
        for r in it["rolls"]:
            if r["chamber"] != chamber:
                continue
            try:
                v = _senate_vote(bioguide, r) if chamber == "Senate" else _house_vote(bioguide, r)
            except Exception:
                v = None
            if v:
                out.append({"item": it, "roll": r, "vote": v, "url": roll_url(r)})
    return out

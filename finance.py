"""'Follow the money' for ballot measure pages.

Data lives in data/measure_finance.json, built by scripts/build_measure_finance.py from each measure's Ballotpedia
campaign-finance section (Ballotpedia compiles state filings; every figure is copied as reported, never estimated).
Entries with source.kind == "official" are built straight from state filings by scripts/build_official_finance.py
(Washington PDC open data, California CAL-ACCESS via Power Search) and are never overwritten by the Ballotpedia build.
Per side ("yes" = committees supporting, "no" = committees opposing) the file holds the side total Ballotpedia reports,
the registered committees, Ballotpedia's top-donor list, and any independent expenditures it lists.

Display rule (same for both sides):
  * show every listed donor who gave THRESHOLD or more;
  * if that leaves fewer than MIN_SHOWN, show the top FALLBACK_TOP listed donors instead, but never a donor below FLOOR
    (keeps small individual givers off the page);
  * never more than MAX_SHOWN rows (the rest are counted and linked to the source).
"""
import json, os

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "data", "measure_finance.json")
THRESHOLD = 100_000
MIN_SHOWN = 3
FALLBACK_TOP = 5
FLOOR = 5_000
MAX_SHOWN = 10
VERIFYING = "Donor details are being verified."
_data = {"mtime": None, "doc": None}


def doc():
    if not os.path.exists(PATH):
        return {"meta": {}, "items": {}}
    m = os.path.getmtime(PATH)
    if _data["doc"] is None or m != _data["mtime"]:
        with open(PATH) as fh:
            _data["doc"], _data["mtime"] = json.load(fh), m
    return _data["doc"]


def pick(donors):
    """(shown, rule) for one side's donor list, sorted largest first. rule is 'threshold' or 'fallback'."""
    ds = sorted(donors, key=lambda d: -d["amount"])
    above = [d for d in ds if d["amount"] >= THRESHOLD]
    if len(above) >= MIN_SHOWN:
        return above[:MAX_SHOWN], "threshold"
    return [d for d in ds[:FALLBACK_TOP] if d["amount"] >= FLOOR][:MAX_SHOWN], "fallback"


def _side(raw, slug, names):
    committees = raw.get("committees", [])
    total = raw.get("total")
    shown, rule = pick(raw.get("donors", []))
    shared = sorted({o for c in committees for o in c.get("also", [])})
    share = None
    if shown and total:
        s = sum(d["amount"] for d in shown) / total
        # A share is only meaningful when every committee on this side works on this measure alone.
        if not shared and s <= 1:
            share = round(s * 100)
    if not committees:
        state = "none"            # no committee registered on this side
    elif not total:
        state = "zero"            # committee(s) registered, $0 reported
    else:
        state = "money"
    csum = round(sum(c["total"] for c in committees), 2)
    donor_note = raw.get("donor_note")
    if raw.get("donors_hidden"):
        shown, share = [], None
        donor_note = donor_note or VERIFYING
    elif shown and total and not shared and sum(d["amount"] for d in shown) > total + 1:
        # Never show donors adding up to more than the side total unless the shared-committee note explains it.
        shown, share, donor_note = [], None, VERIFYING
    names_shown = [d["name"].lower() for d in shown]
    return {"state": state, "total": total, "committees": committees, "donors": shown, "rule": rule,
            "donor_note": donor_note, "total_note": raw.get("total_note"),
            "same_name": len(set(names_shown)) < len(names_shown),
            "filer_links": any(d.get("filer_url") for d in shown),
            "listed": len(raw.get("donors", [])), "share": share,
            "shared_with": [names.get(o, o) for o in shared],
            "sum_differs": bool(committees) and total is not None and abs(csum - total) >= 1 and not raw.get("total_note"),
            "ie": sorted(raw.get("ie", []), key=lambda d: -d["amount"])}


def get(slug, names=None):
    """View model for one measure, or None if the file has no entry for it."""
    raw = doc()["items"].get(slug)
    if raw is None:
        return None
    names = names or {}
    out = dict(raw)
    out["yes"] = _side(raw.get("yes", {}), slug, names)
    out["no"] = _side(raw.get("no", {}), slug, names)
    out["empty"] = all(out[s]["state"] == "none" and not out[s]["ie"] for s in ("yes", "no"))
    out["threshold"] = THRESHOLD
    out["floor"] = FLOOR
    return out


def money(v):
    """$1,234,567, rounded to the nearest dollar (the page says so)."""
    if v is None:
        return "—"
    return f"${v:,.0f}"


def as_of_label(iso):
    if not iso:
        return None
    import datetime
    d = datetime.date.fromisoformat(iso)
    mon = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."][d.month - 1]
    return f"{mon} {d.day}, {d.year}"

"""Local officials (city / county) from data/local_officials.json, keyed by Census GEOID.

The file is built by scripts/build_local_officials.py from free open data (openstates/people,
Wikidata, Wikipedia infobox names, Census Gazetteer). Only entries whose status is
"confirmed" or "corroborated" are shown; "unverified" entries stay in the file but are hidden.
"""
import json, os, re, datetime

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "local_officials.json")
SHOW = ("confirmed", "corroborated")
try:
    with open(_PATH, encoding="utf-8") as f:
        DATA = json.load(f)
except (OSError, ValueError):
    DATA = {"meta": {}, "places": {}, "counties": {}}


def _party_class(p):
    p = (p or "").strip().lower()
    return "R" if p.startswith("republican") else "D" if p.startswith("democrat") else "N"


def _fmt_date(s):
    try:
        d = datetime.date.fromisoformat((s or "")[:10])
        return d.strftime("%b %Y") if len((s or "")) >= 7 else str(d.year)
    except ValueError:
        return (s or "")[:4] or None


def _term(o):
    a, b = _fmt_date(o.get("term_start")), _fmt_date(o.get("term_end"))
    if a and b:
        return f"Term {a} – {b}"
    if b:
        return f"Term ends {b}"
    if a:
        return f"In office since {a}"
    return None


def _officials(bucket, geoid):
    rec = (DATA.get(bucket) or {}).get(geoid)
    if not rec:
        return []
    out = []
    for o in rec["officials"]:
        if o.get("status") not in SHOW:
            continue
        if re.search(r"vice|pro tem|administrator|deputy", o["title"], re.I):
            continue   # only the head of government; not deputies or appointed staff
        title = " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in o["title"].split())
        out.append({"name": o["name"], "title": title, "party": o.get("party"), "pclass": _party_class(o.get("party")),
                    "term": _term(o), "source_url": o["source_url"], "listed_by": o.get("listed_by"),
                    "confirmed": o["status"] == "confirmed", "last_verified": o.get("last_verified")})
    out.sort(key=lambda x: (not x["confirmed"], x["name"]))
    return out


def for_geo(geo):
    """Return {"jurisdictions": [...], "school_districts": [...], "last_verified": ..., "partial": bool}."""
    js = []
    if geo.get("place_geoid"):
        js.append({"label": "City / town", "name": geo.get("place_full") or geo.get("place"), "officials": _officials("places", geo["place_geoid"])})
    elif geo.get("cdp_geoid"):
        js.append({"label": "City / town", "name": geo.get("cdp_name"), "unincorporated": True, "officials": []})
    if geo.get("county_geoid"):
        js.append({"label": "County", "name": geo.get("county"), "officials": _officials("counties", geo["county_geoid"])})
    return {"jurisdictions": js, "school_districts": geo.get("school_districts") or [],
            "last_verified": (DATA.get("meta") or {}).get("last_verified"),
            "missing": [j for j in js if not j["officials"]]}

"""Two-sided summaries of statewide ballot measures (Nov. 3, 2026).

Data lives in data/measures_bothsides.json (hand-written; every quote and figure is checked against the cited
source text before the file is built; see README "Ballot measures"). Same pattern as bothsides.py: load, number each
item's sources for footnotes, and match entries from the Elections panel (data/elections_2026.json) by Ballotpedia URL.
"""
import json, os

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "data", "measures_bothsides.json")
_data = {"mtime": 0, "doc": None}


def doc():
    m = os.path.getmtime(PATH)
    if _data["doc"] is None or m != _data["mtime"]:
        d = json.load(open(PATH))
        d["_by_id"] = {i["id"]: i for i in d["items"]}
        d["_by_url"] = {i["ballotpedia_url"].rstrip("/"): i for i in d["items"]}
        _data["doc"], _data["mtime"] = d, m
    return _data["doc"]


def meta():
    return doc()["meta"]


def items():
    return doc()["items"]


def get(slug):
    return doc()["_by_id"].get(slug)


def match(url):
    """The summary for a statewide-measure entry from the Elections panel (matched by its Ballotpedia URL)."""
    return doc()["_by_url"].get((url or "").rstrip("/"))


def by_state():
    """[(state_code, state_name, [items])] sorted by state name; items keep their data order within a state."""
    out = {}
    for it in items():
        out.setdefault((it["state"], it["state_name"]), []).append(it)
    return [(c, n, v) for (c, n), v in sorted(out.items(), key=lambda kv: kv[0][1])]


def for_state(code):
    return [i for i in items() if i["state"] == code]


def sources(item):
    """Ordered [(n, id, source)] for every source an item cites, numbered by first use."""
    S = doc()["sources"]
    order = []
    cited = list(item.get("summary_src", []))
    for key in ("fiscal", "money"):
        if item.get(key):
            cited += item[key]["src"]
    for side in ("for", "against", "unsaid"):
        for p in item.get(side, []):
            cited += p["src"]
    for sid in cited:
        if sid not in order:
            order.append(sid)
    return [(n + 1, sid, S[sid]) for n, sid in enumerate(order)]


def source_numbers(item):
    return {sid: n for n, sid, _ in sources(item)}


def all_sources():
    return [(it, sources(it)) for it in items()]

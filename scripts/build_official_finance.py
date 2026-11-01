"""Replace selected measures in data/measure_finance.json with figures built straight from official filings.

  python scripts/build_official_finance.py --wa-cache DIR [--fetch] --ca-dir DIR

Washington: itemized contributions from the Public Disclosure Commission open data set on data.wa.gov
  (kv7h-kjye, "Contributions to Candidates and Political Committees"), 2025 and 2026 election years, for the committees
  registered on each measure. Side total = sum of those committees' reported contributions, minus money one committee
  on the side gave another (so it is not counted twice).
California: CAL-ACCESS contributions exported from the Secretary of State's Power Search (advanced search, ballot
  measure + position, "exclude contributions between allied committees"), one CSV per measure and side,
  named ps_<prop>_<S|O>.csv in --ca-dir.

Donors are summed per contributor. Two filings are combined only when the contributor name matches and the address is
the same after normalising abbreviations (ST/STREET, STE/SUITE, #/UNIT ...) or, for California, the CAL-ACCESS
committee ID or name + ZIP match. Nothing is estimated. Entries written here carry source.kind = "official"; the
Ballotpedia builder leaves them alone.
"""
import argparse, collections, csv, datetime, json, os, re, sys, urllib.parse, urllib.request

KEEP = 25            # donors stored per side (largest first, $5,000 or more); the page shows at most 10
KEEP_FLOOR = 5_000
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "data", "measure_finance.json")
TODAY = datetime.date.today().isoformat()

# ---------------------------------------------------------------- Washington
WA_DATASET = "https://data.wa.gov/resource/kv7h-kjye.json"
WA_DATASET_PAGE = "https://data.wa.gov/Politics/Contributions-to-Candidates-and-Political-Committe/kv7h-kjye"
PDC_COMM = "https://www.pdc.wa.gov/political-disclosure-reporting-data/browse-search-data/committees/co-2026-{}"
WA_COMM = {  # filer_id: (display name, PDC committee id)
    "HEYWL--535": ("Let's Go Washington", "30644"),
    "TAX-V--889": ("Vote Yes Repeal the Income Tax", "42237"),
    "645-N--960": ("No on 645", "42211"),
    "WASHD--788": ("Defend Washington", "35835"),
    "WASHM--071": ("Millionaire's Tax Washington", "42119"),
    "FREEW--138": ("No Hate in WA State", "38851"),
    "PERMDP 073": ("Permanent Defense PAC (Wrong for Washington)", "15151"),
}
WA_MEASURES = {
    "washington-initiative-645": {"yes": ["HEYWL--535", "TAX-V--889"], "no": ["645-N--960", "WASHD--788", "WASHM--071", "PERMDP 073"]},
    "washington-initiative-1": {"yes": ["HEYWL--535"], "no": ["FREEW--138", "WASHD--788", "PERMDP 073"]},
    "washington-initiative-638": {"yes": ["HEYWL--535"], "no": ["FREEW--138", "WASHD--788", "PERMDP 073"]},
}
WA_YEARS = ("2025", "2026")

ABBR = {"STREET": "ST", "SUITE": "STE", "AVENUE": "AVE", "DRIVE": "DR", "PLACE": "PL", "ROAD": "RD", "BOULEVARD": "BLVD",
        "UNIT": "#", "APT": "#", "WAY": "WAY", "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}


def norm_name(s):
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper()).split())


def norm_addr(a):
    a = (a or "").upper().replace("#", " # ")
    toks = [ABBR.get(t, t) for t in re.sub(r"[^A-Z0-9# ]", " ", a).split()]
    out = []
    for t in toks:          # "# 713" and "713" after a street suffix are the same unit
        if t == "#":
            continue
        out.append(t)
    return " ".join(out)


def addr_without_unit(a):
    return re.sub(r"\s+(STE|#)?\s*\d+[A-Z]?$", "", norm_addr(a)).replace(" STE", "")


def person(name, category):
    """Names are shown as filed (filers use both 'Last First' and 'First Last', so they are not reordered); all-caps
    names are title-cased."""
    n = " ".join(name.split())
    return n.title() if n.isupper() else n


def trim(donors):
    """Keep the largest donors only; same-name flags are kept only when both entries survive."""
    kept = [d for d in donors if d["amount"] >= KEEP_FLOOR][:KEEP]
    names = collections.Counter(d["name"].lower() for d in kept)
    for d in kept:
        if names[d["name"].lower()] < 2:
            d.pop("separate_address", None)
    return kept


def wa_rows(cache, fetch):
    path = os.path.join(cache, "wa_rows.json")
    if fetch or not os.path.exists(path):
        ids = ",".join(f"'{i}'" for i in WA_COMM)
        q = urllib.parse.urlencode({"$where": f"filer_id in ({ids}) AND election_year in ({','.join(repr(y) for y in WA_YEARS)})",
                                    "$limit": "100000"})
        with urllib.request.urlopen(f"{WA_DATASET}?{q}", timeout=120) as r:
            data = json.load(r)
        with open(path, "w") as fh:
            json.dump(data, fh)
    with open(path) as fh:
        return json.load(fh)


def merge_donors(entries):
    """entries: list of (name_key, addr, display, amount). Combine only on same name + same address; organisations also
    on same name + same street address with the suite left off. Returns donors and same-name rows left separate."""
    g = collections.OrderedDict()
    for nk, addr, disp, amt, is_org in entries:
        k = (nk, norm_addr(addr))
        if k not in g:
            alt = [kk for kk in g if kk[0] == nk and is_org and addr_without_unit(kk[1]) == addr_without_unit(addr)]
            if alt:
                k = alt[0]
            else:
                g[k] = {"name": disp, "amount": 0.0, "rows": 0}
        g[k]["amount"] += amt
        g[k]["rows"] += 1
    by = collections.Counter(k[0] for k in g)
    out = []
    for k, d in g.items():
        d["amount"] = round(d["amount"], 2)
        if by[k[0]] > 1:
            d["separate_address"] = True
        if d["rows"] == 1:
            d.pop("rows")
        out.append(d)
    return sorted(out, key=lambda d: -d["amount"])


def build_wa(rows):
    items = {}
    names = {fid: v[0] for fid, v in WA_COMM.items()}
    filer_norm = {norm_name(r["filer_name"]): r["filer_id"] for r in rows}
    filer_norm.update({norm_name(v[0]): k for k, v in WA_COMM.items()})
    last = max(r.get("receipt_date", "") for r in rows)[:10]
    for slug, sides in WA_MEASURES.items():
        it = {"as_of": last,
              "source": {"org": "Washington Public Disclosure Commission", "kind": "official",
                         "title": "Contributions to Candidates and Political Committees (open data)",
                         "url": WA_DATASET_PAGE, "accessed": TODAY,
                         "method": "Itemized contributions reported to each committee for the 2025 and 2026 election years."},
              "filings": {"org": "Washington Public Disclosure Commission",
                          "url": "https://www.pdc.wa.gov/political-disclosure-reporting-data/browse-search-data/committees"}}
        for side, ids in sides.items():
            rs = [r for r in rows if r["filer_id"] in ids]
            comms = []
            for fid in ids:
                others = sorted(s for s, sd in WA_MEASURES.items() if s != slug and any(fid in v for v in sd.values()))
                c = {"name": names[fid], "total": round(sum(float(r["amount"]) for r in rs if r["filer_id"] == fid), 2),
                     "url": PDC_COMM.format(WA_COMM[fid][1])}
                if others:
                    c["also"] = others
                comms.append(c)
            transfers, entries, small = [], [], 0.0
            for r in rs:
                nm = r.get("contributor_name", "")
                src = filer_norm.get(norm_name(nm))
                if src in ids and src != r["filer_id"]:
                    transfers.append((names[src], names[r["filer_id"]], float(r["amount"])))
                    continue
                if nm.upper() == "SMALL CONTRIBUTIONS":
                    small += float(r["amount"])
                    continue
                entries.append((norm_name(nm), r.get("contributor_address"), person(nm, r.get("contributor_category")),
                                float(r["amount"]), r.get("contributor_category") != "Individual"))
            gross = round(sum(c["total"] for c in comms), 2)
            xfer = round(sum(t[2] for t in transfers), 2)
            s = {"committees": comms, "donors": trim(merge_donors(entries)), "total": round(gross - xfer, 2), "ie": []}
            if xfer:
                pairs = sorted({(a, b) for a, b, _ in transfers})
                s["total_note"] = ("The total leaves out " + "; ".join(
                    f"${sum(t[2] for t in transfers if (t[0], t[1]) == p):,.0f} {p[0]} gave to {p[1]}" for p in pairs)
                    + ", so that money is not counted twice.")
            items.setdefault(slug, dict(it))[side] = s
    return items


# ---------------------------------------------------------------- California
CA_COMM_URL = "https://cal-access.sos.ca.gov/Campaign/Committees/Detail.aspx?id={}"
CA_PROPS = {"california-proposition-40": "40", "california-proposition-41": "41", "california-proposition-42": "42"}
CA_NAMES = {
    "1484470": "Yes on 40 - Billionaire Tax Now - No on 41 & 42",
    "1495036": "Tax the Ultra-Rich Now, YES on 3 & 40, NO on 41 & 42",
    "1497412": "Working Families Say No to Propositions 41 and 42",
    "1492108": "No on Prop 40 - Doctors, Teachers, Small Businesses, Working Families and Public Safety Against the Reckless Wealth Tax Experiment",
    "1486695": "Golden State Promise - No on Prop 40",
    "1485228": "Californians Against Tax Increases, sponsored by the California Business Roundtable - No on 40",
    "1485007": "Stop the Squeeze - No on Prop 40",
    "1488423": "Yes on 41 - Californians for Transparency and Accountability",
    "1488418": "Yes on 42 - Protect Retirement & Life Savings",
    "1486767": "Building a Better California",
    "911908": "Peace Officers Research Association of California Political Issues Committee (PORAC PIC)",
    "1268914": "Reform California with Carl DeMaio - Ballot Measure Committee",
    "1452276": "Reform Local Government PAC",
    "1483288": "Right Path California Issues PAC",
    "1264590": "California Business Roundtable Issues PAC",
    "1291923": "Million More Voters, sponsored by the California Labor Federation, AFL-CIO",
}
PROP_SLUG = {"003": "california-proposition-3", "040": "california-proposition-40", "041": "california-proposition-41",
             "042": "california-proposition-42"}


def ca_person(name, cid=""):
    """CAL-ACCESS lists individuals as 'LAST, FIRST'; organisation names are left as filed."""
    n = " ".join(name.split())
    if "," in n and not re.search(r"\b(INC|LLC|PAC|COMMITTEE|UNION|ASSOCIATION|FUND|LABS|SPONSORED|AFL|CIO|501)\b", n.upper()):
        last, first = n.split(",", 1)
        n = f"{first.strip()} {last.strip()}"
    if n.isupper():
        n = n.title()
    return n


def build_ca(ca_dir):
    items = {}
    asof = None
    for slug, num in CA_PROPS.items():
        it = {"source": {"org": "California Secretary of State, CAL-ACCESS", "kind": "official",
                         "title": "Power Search: contributions to committees supporting and opposing Proposition " + num,
                         "url": "https://powersearch.sos.ca.gov/advanced.php", "accessed": TODAY,
                         "method": "Contributions to the committees that report supporting or opposing the measure, with transfers between allied committees left out."},
              "filings": {"org": "California Secretary of State, CAL-ACCESS", "url": "https://cal-access.sos.ca.gov/Campaign/Measures/"}}
        for side, pos in (("yes", "S"), ("no", "O")):
            path = os.path.join(ca_dir, f"ps_{num}_{pos}.csv")
            rows = []
            with open(path, encoding="utf-8-sig", errors="ignore") as fh:
                for r in csv.DictReader(fh):
                    try:
                        r["_amt"] = float(r["Amount"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    rows.append(r)
            asof = max(asof or "", max(r["End Date"] for r in rows))
            cids = list(dict.fromkeys(r["Recipient Committee ID"] for r in rows))
            comms = []
            for cid in cids:
                rr = [r for r in rows if r["Recipient Committee ID"] == cid]
                ms = set(re.findall(r"(?:SUPPORTED|OPPOSED): PROPOSITION (\d+)", " ".join(r["Ballot Measure(s)"] for r in rr)))
                c = {"name": CA_NAMES.get(cid, rr[0]["Recipient Committee"]), "total": round(sum(r["_amt"] for r in rr), 2),
                     "url": CA_COMM_URL.format(cid)}
                also = sorted(PROP_SLUG.get(m, f"california-proposition-{int(m)}") for m in ms if int(m) != int(num))
                if also:
                    c["also"] = also
                comms.append(c)
            comms.sort(key=lambda c: -c["total"])
            g = collections.OrderedDict()
            xfer = 0.0
            for r in rows:
                cid = r["Contributor ID"].strip()
                if cid and cid in cids:          # one committee on this side paying another
                    xfer += r["_amt"]
                    continue
                if r["Contributor Name"].strip().lower() == "unitemized contributions":
                    continue
                k = ("id", cid) if cid else ("nm", norm_name(r["Contributor Name"]), r["Contributor Zip Code"][:5])
                if k not in g:
                    d = {"name": CA_NAMES.get(cid) or ca_person(r["Contributor Name"], cid), "amount": 0.0, "rows": 0}
                    if cid:
                        d["filer_url"] = CA_COMM_URL.format(cid)
                    g[k] = d
                g[k]["amount"] += r["_amt"]
                g[k]["rows"] += 1
            donors = []
            for d in g.values():
                d["amount"] = round(d["amount"], 2)
                if d["rows"] == 1:
                    d.pop("rows")
                donors.append(d)
            donors.sort(key=lambda d: -d["amount"])
            s = {"committees": comms, "donors": trim(donors), "total": round(sum(c["total"] for c in comms) - xfer, 2), "ie": []}
            if xfer:
                s["total_note"] = f"The total leaves out ${xfer:,.0f} that committees on this side gave each other, so it is not counted twice."
            it[side] = s
        items[slug] = it
    for it in items.values():
        it["as_of"] = asof
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wa-cache")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--ca-dir")
    ap.add_argument("--ca-as-of", help="'Contributions data is current as of' date shown by Power Search (YYYY-MM-DD)")
    a = ap.parse_args()
    with open(PATH) as fh:
        doc = json.load(fh)
    if a.wa_cache:
        for slug, it in build_wa(wa_rows(a.wa_cache, a.fetch)).items():
            doc["items"][slug] = it
            print(slug, it["as_of"], it["yes"]["total"], it["no"]["total"])
    if a.ca_dir:
        for slug, it in build_ca(a.ca_dir).items():
            if a.ca_as_of:
                it["as_of"] = a.ca_as_of
            doc["items"][slug] = it
            print(slug, it["as_of"], it["yes"]["total"], it["no"]["total"])
    with open(PATH, "w") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()

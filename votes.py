"""Recent roll-call votes for federal members, keyless.

House:  House Clerk XML (clerk.house.gov/evs/<year>/rollNNN.xml), one file per vote, cached forever once fetched.
Senate: Voteview (UCLA) bulk CSVs, because www.senate.gov's XML blocked the box this was built on (HTTP 403 from Akamai).
        Voteview is also the House fallback if the Clerk is unreachable.
Everything is cached on disk under cache/ and failures degrade to an empty list plus an error message.
"""
import csv, datetime, io, json, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
os.makedirs(os.path.join(CACHE, "house"), exist_ok=True)
os.makedirs(os.path.join(CACHE, "voteview"), exist_ok=True)
UA = {"User-Agent": "weelect-prototype/0.1 (contact: site owner)"}
CLERK = "https://clerk.house.gov/evs/{year}/roll{num:03d}.xml"
VV = "https://voteview.com/static/data/out/{kind}/HS119_{kind}.csv"
SCAN = 60            # how many recent roll calls to scan per chamber
SHOW = 15            # how many votes to show on a profile
_lock = threading.Lock()
_mem = {}

TYPE_PATH = {"HR": "house-bill", "S": "senate-bill", "HRES": "house-resolution", "SRES": "senate-resolution",
             "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
             "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution"}


def bill_url(label, congress=119):
    """'H R 5103' / 'HR5103' / 'S 12' -> congress.gov URL, else None (nominations, quorum calls...)."""
    m = re.match(r"^([A-Za-z .]+?)\s*(\d+)$", (label or "").strip())
    if not m:
        return None
    t = re.sub(r"[^A-Z]", "", m.group(1).upper())
    if t in TYPE_PATH:
        return f"https://www.congress.gov/bill/{congress}th-congress/{TYPE_PATH[t]}/{m.group(2)}"
    return None


def _get(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


# ---------------- House Clerk ----------------
def _house_roll(year, num):
    """Parsed roll call dict or None if it doesn't exist. Cached forever on disk."""
    path = os.path.join(CACHE, "house", f"{year}_{num}.json")
    if os.path.exists(path):
        return json.load(open(path))
    r = requests.get(CLERK.format(year=year, num=num), headers=UA, timeout=25)
    if r.status_code != 200 or b"<rollcall-vote" not in r.content[:400]:
        return None
    root = ET.fromstring(r.content)
    md = root.find("vote-metadata")
    txt = lambda e, t: (e.findtext(t) or "").strip()
    d = {"year": year, "num": num, "congress": int(txt(md, "congress") or 119),
         "legis": txt(md, "legis-num"), "question": txt(md, "vote-question"), "type": txt(md, "vote-type"),
         "result": txt(md, "vote-result"), "desc": txt(md, "vote-desc"), "date_raw": txt(md, "action-date"),
         "votes": {}}
    try:
        d["date"] = datetime.datetime.strptime(d["date_raw"], "%d-%b-%Y").date().isoformat()
    except ValueError:
        d["date"] = ""
    for rv in root.iter("recorded-vote"):
        leg = rv.find("legislator")
        if leg is not None and leg.get("name-id"):
            d["votes"][leg.get("name-id")] = (rv.findtext("vote") or "").strip()
    json.dump(d, open(path, "w"))
    return d


def _house_latest(year):
    """Highest existing roll number for the year (0 if none). Exponential + binary search, cached 30 min."""
    key = ("latest", year)
    c = _mem.get(key)
    if c and time.time() - c[0] < 1800:
        return c[1]
    exists = lambda n: _house_roll_exists(year, n)
    if not exists(1):
        res = 0
    else:
        lo, hi = 1, 2
        while exists(hi):
            lo, hi = hi, hi * 2
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if exists(mid): lo = mid
            else: hi = mid
        res = lo
    _mem[key] = (time.time(), res)
    return res


def _house_roll_exists(year, n):
    if os.path.exists(os.path.join(CACHE, "house", f"{year}_{n}.json")):
        return True
    r = requests.get(CLERK.format(year=year, num=n), headers=UA, timeout=25)
    return r.status_code == 200 and b"<rollcall-vote" in r.content[:400]


def _house_recent_rolls(n=SCAN):
    year = datetime.date.today().year
    out = []
    for y in (year, year - 1):
        latest = _house_latest(y)
        nums = list(range(latest, max(0, latest - (n - len(out))), -1))
        with ThreadPoolExecutor(8) as ex:
            rolls = list(ex.map(lambda k: _house_roll(y, k), nums))
        out += [r for r in rolls if r]
        if len(out) >= n:
            break
    return out


def house_votes(bioguide):
    rolls = _house_recent_rolls()
    res = []
    for r in rolls:
        if r["type"] == "QUORUM" or "Call of the House" in r["question"]:
            continue
        v = r["votes"].get(bioguide)
        if v is None:
            continue
        res.append({"date": r["date"], "label": r["legis"], "question": r["question"], "result": r["result"],
                    "desc": r["desc"], "vote": v,
                    "source": f"https://clerk.house.gov/Votes/{r['year']}{r['num']}",
                    "source_name": "House Clerk", "bill_url": bill_url(r["legis"], r["congress"])})
        if len(res) >= SHOW:
            break
    return res


# ---------------- Voteview (Senate; House fallback) ----------------
CAST = {1: "Yea", 2: "Yea (paired)", 3: "Yea (announced)", 4: "Nay (announced)", 5: "Nay (paired)",
        6: "Nay", 7: "Present", 8: "Present", 9: "Not Voting"}


def _vv_file(kind):
    path = os.path.join(CACHE, "voteview", f"{kind}.csv")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 12 * 3600:
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(_get(VV.format(kind=kind).replace("/out/" + kind + "/", f"/out/{kind}/"), 180).content)
            os.replace(tmp, path)
        except Exception:
            if not os.path.exists(path):
                raise
    return path


def _vv_data(chamber):
    key = ("vv", chamber)
    c = _mem.get(key)
    if c and time.time() - c[0] < 12 * 3600:
        return c[1]
    members = {}
    for r in csv.DictReader(open(_vv_file("members"))):
        if r["chamber"] == chamber and r["bioguide_id"]:
            members[r["bioguide_id"]] = r["icpsr"]
    rolls = {}
    for r in csv.DictReader(open(_vv_file("rollcalls"))):
        if r["chamber"] == chamber:
            rolls[int(r["rollnumber"])] = r
    recent = sorted(rolls)[-SCAN * 3:]
    keep = set(recent)
    casts = {}
    for r in csv.DictReader(open(_vv_file("votes"))):
        if r["chamber"] == chamber and int(r["rollnumber"]) in keep:
            casts[(r["icpsr"], int(r["rollnumber"]))] = int(float(r["cast_code"]))
    data = (members, rolls, recent, casts)
    _mem[key] = (time.time(), data)
    return data


def voteview_votes(bioguide, chamber):
    members, rolls, recent, casts = _vv_data(chamber)
    icpsr = members.get(bioguide)
    if not icpsr:
        return []
    res = []
    for n in reversed(recent):
        cc = casts.get((icpsr, n))
        r = rolls[n]
        if cc is None or "Call of the House" in (r["vote_question"] or "") or r["vote_desc"] == "QUORUM":
            continue
        if chamber == "Senate":
            src = (f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote119{r['session']}/"
                   f"vote_119_{r['session']}_{int(r['clerk_rollnumber']):05d}.xml")
            sname = "Senate.gov (via Voteview)"
        else:
            src = f"https://voteview.com/rollcall/RH119{n:04d}"
            sname = "Voteview"
        res.append({"date": r["date"], "label": r["bill_number"], "question": r["vote_question"],
                    "result": r["vote_result"], "desc": r["vote_desc"] or r["dtl_desc"],
                    "vote": CAST.get(cc, str(cc)), "source": src, "source_name": sname,
                    "bill_url": bill_url(r["bill_number"])})
        if len(res) >= SHOW:
            break
    return res


# ---------------- public entry ----------------
def recent_votes(bioguide, chamber_type):
    """chamber_type: 'rep' or 'sen'. Returns (votes, error_message_or_None, note_or_None)."""
    if chamber_type == "sen":
        try:
            return voteview_votes(bioguide, "Senate"), None, "Senate votes come from Voteview (UCLA), which republishes official Senate roll calls; each vote links to the Senate.gov record."
        except Exception as e:
            return [], f"Could not load Senate votes ({type(e).__name__}).", None
    try:
        return house_votes(bioguide), None, None
    except Exception as e:
        try:
            return voteview_votes(bioguide, "House"), None, "House Clerk was unreachable, so these come from Voteview (UCLA)."
        except Exception as e2:
            return [], f"Could not load House votes ({type(e).__name__} / {type(e2).__name__}).", None

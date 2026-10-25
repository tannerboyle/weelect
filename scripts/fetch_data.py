"""Download/refresh the free bulk data used by the prototype (all CC0).

- unitedstates/congress-legislators  -> data/legislators-current.json, legislators-social-media.json
- openstates/people (shallow clone)  -> data/state_people.json (compact index: state legislators + a few mayors)

Usage: python scripts/fetch_data.py
"""
import json, os, subprocess, sys, tempfile, shutil, datetime
import requests, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA, exist_ok=True)

for name in ("legislators-current.json", "legislators-social-media.json"):
    r = requests.get("https://unitedstates.github.io/congress-legislators/" + name, timeout=60)
    r.raise_for_status()
    with open(os.path.join(DATA, name), "wb") as f:
        f.write(r.content)
    print("fetched", name)

# Federal member photos (225x275) -> static/photos/<bioguide>.jpg, skipping ones already downloaded
from concurrent.futures import ThreadPoolExecutor
PH = os.path.join(ROOT, "static", "photos")
os.makedirs(PH, exist_ok=True)
def grab(bio):
    p = os.path.join(PH, bio + ".jpg")
    if os.path.exists(p):
        return 0
    r = requests.get(f"https://unitedstates.github.io/images/congress/225x275/{bio}.jpg", timeout=30)
    if r.status_code == 200:
        open(p, "wb").write(r.content)
        return 1
    return 0
bios = [l["id"]["bioguide"] for l in json.load(open(os.path.join(DATA, "legislators-current.json")))]
with ThreadPoolExecutor(8) as ex:
    print("photos downloaded:", sum(ex.map(grab, bios)))

tmp = tempfile.mkdtemp()
try:
    subprocess.check_call(["git", "clone", "--depth", "1", "-q",
                           "https://github.com/openstates/people.git", tmp])
    today = datetime.date.today().isoformat()
    out = []
    base = os.path.join(tmp, "data")
    for st in sorted(os.listdir(base)):
        if st == "us":
            continue
        for sub in ("legislature", "municipalities"):
            d = os.path.join(base, st, sub)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".yml"):
                    continue
                p = yaml.safe_load(open(os.path.join(d, fn)))
                roles = []
                for r in p.get("roles") or []:
                    end = str(r.get("end_date") or "")
                    if end and end < today:
                        continue
                    j = r.get("jurisdiction", "")
                    place = ""
                    if "/place:" in j:
                        place = j.split("/place:")[1].split("/")[0]
                    roles.append({"type": r.get("type"), "district": str(r.get("district", "")),
                                  "place": place})
                if not roles:
                    continue
                party = (p.get("party") or [{}])[-1].get("name", "")
                out.append({
                    "id": p["id"].replace("ocd-person/", ""),
                    "name": p["name"], "state": st.upper(), "party": party,
                    "email": p.get("email"), "image": p.get("image"),
                    "roles": roles,
                    "links": [l["url"] for l in p.get("links") or []],
                    "offices": [{k: o.get(k) for k in ("classification", "address", "voice")}
                                for o in p.get("offices") or []],
                    "ballotpedia": next((s["url"] for s in (p.get("sources") or [])
                                         if "ballotpedia.org" in s["url"]), None),
                })
    json.dump(out, open(os.path.join(DATA, "state_people.json"), "w"), separators=(",", ":"))
    print("state/municipal people indexed:", len(out))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

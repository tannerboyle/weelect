"""Build data/elections_2026.json for the "Elections" panel (Nov. 3, 2026 general election).

Needs: pip install beautifulsoup4   (build-time only; the web app itself does not need it)

Sources (all public web pages; every value is stored with its source so the panel can label it):
  * Ballotpedia   - poll hours, early voting dates, registration / absentee deadlines (each row cites the
                    state election office page), statewide ballot measures, per-state election pages.
  * Wikipedia     - which state legislative seats are up in 2026 (table "State legislative elections").
  * Wikipedia     - which states elect a governor in 2026 (see GOVERNOR_STATES below; 36 states).
  * Official state "where do I vote" lookup URLs (LOOKUP_URLS below), curated and link-checked by hand.
Federal seats up are NOT stored here: app.py derives them from congress-legislators term end dates.

Usage: python scripts/fetch_elections.py
"""
import datetime, json, os, re, sys
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) WeElect data refresh (weelect.org)"}
ELECTION_DAY = "Nov. 3, 2026"
BP = "https://ballotpedia.org/"

STATES = {
 "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
 "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
 "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
 "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
 "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
 "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
 "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
 "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
 "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
 "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

# 36 states electing a governor on Nov. 3, 2026.
# Source: https://en.wikipedia.org/wiki/2026_United_States_gubernatorial_elections (checked 2026-10-26).
# (D.C. also elects a mayor on Nov. 3, 2026; handled separately in the app.)
GOVERNOR_STATES = ("AL AK AZ AR CA CO CT FL GA HI ID IL IA KS ME MD MA MI MN NE NV NH NM NY OH OK OR PA RI "
                   "SC SD TN TX VT WI WY").split()
assert len(GOVERNOR_STATES) == 36

# Official "find my polling place / where do I vote" tool for each state.
# Each URL was opened (HTTP 200 or a page that loads in a browser) on 2026-10-26. The ones marked
# "listed" were listed by Ballotpedia and the state's own pages but block automated checks (Cloudflare/WAF).
LOOKUP_URLS = {
 "AL": "https://myinfo.alabamavotes.gov/VoterView/PollingPlaceSearch.do",
 "AK": "https://myvoterinformation.alaska.gov/",                       # listed
 "AZ": "https://my.arizona.vote/WhereToVote.aspx",
 "AR": "https://www.voterview.ar-nova.org/voterview",
 "CA": "https://voterstatus.sos.ca.gov/",
 "CO": "https://www.coloradosos.gov/voter/pages/pub/olvr/findVoterReg.xhtml",
 "CT": "https://portal.ct.gov/SOTS/Election-Services/Voter-Information/Where-and-how-do-I-vote",
 "DE": "https://ivote.de.gov/VoterView",
 "DC": "https://dcboe.org/voters/find-out-where-to-vote/vote-center-locator-tool",
 "FL": "https://registration.dos.fl.gov/CheckVoterStatus",
 "GA": "https://mvp.sos.ga.gov/",
 "HI": "https://olvr.hawaii.gov/",
 "ID": "https://votertools.voteidaho.gov/TEDElectionLink/VoterServices/voter/where-do-I-vote",
 "IL": "https://ova.elections.il.gov/RegistrationLookup.aspx",
 "IN": "https://indianavoters.in.gov/",
 "IA": "https://voterready.iowa.gov/",                                 # listed
 "KS": "https://myvoteinfo.voteks.org/VoterView/PollingPlaceSearch.do",
 "KY": "https://vrsws.sos.ky.gov/vic/",                                # listed
 "LA": "https://voterportal.sos.la.gov/",
 "ME": "https://www.maine.gov/portal/government/edemocracy/voter_lookup.php",
 "MD": "https://voterservices.elections.maryland.gov/PollingPlaceSearch",
 "MA": "https://www.sec.state.ma.us/wheredoivotema/bal/",
 "MI": "https://mvic.sos.state.mi.us/",
 "MN": "https://pollfinder.sos.mn.gov/",
 "MS": "https://myelectionday.sos.state.ms.us/VoterOutreach/Pages/VOSearch.aspx",
 "MO": "https://voteroutreach.sos.mo.gov/portal",
 "MT": "https://voterportal.mt.gov/WhereToVote.aspx",
 "NE": "https://www.votercheck.necvr.ne.gov/voterview/",
 "NV": "https://www.nvsos.gov/votersearch/",
 "NH": "https://app.sos.nh.gov/pollingplacesampleballot",             # listed
 "NJ": "https://voter.svrs.nj.gov/polling-place-search",
 "NM": "https://voterportal.servis.sos.state.nm.us/WhereToVote.aspx",
 "NY": "https://voterlookup.elections.ny.gov/",
 "NC": "https://vt.ncsbe.gov/PPLkup/",
 "ND": "https://vip.sos.nd.gov/wheretovote.aspx",
 "OH": "https://voterlookup.ohiosos.gov/",                            # listed
 "OK": "https://okvoterportal.okelections.gov/",
 "OR": "https://sos.oregon.gov/elections/pages/myvote.aspx",
 "PA": "https://www.pavoterservices.pa.gov/Pages/PollingPlaceInfo.aspx",
 "RI": "https://vote.sos.ri.gov/Home/PollingPlaces",                   # listed
 "SC": "https://scvotes.gov/voters/",
 "SD": "https://vip.sdsos.gov/viplogin.aspx",
 "TN": "https://tnmap.tn.gov/voterlookup/",
 "TX": "https://teamrv-mvp.sos.texas.gov/MVP/mvp.do",
 "UT": "https://votesearch.utah.gov/voter-search/search/search-by-address/how-and-where-can-i-vote",
 "VT": "https://vote.vermont.gov/",
 "VA": "https://vote.elections.virginia.gov/VoterInformation",
 "WA": "https://voter.votewa.gov/WhereToVote.aspx",
 "WV": "https://apps.sos.wv.gov/Elections/voter/FindMyPollingPlace",
 "WI": "https://myvote.wi.gov/en-us/Find-My-Polling-Place",
 "WY": "https://myelectionday.sos.wyo.gov/WYVOTES/Pages/VOSearch.aspx",
}
assert set(LOOKUP_URLS) == set(STATES.values())

MEASURE_TYPES = {
    "LRCA": "Legislatively referred constitutional amendment", "LRSS": "Legislatively referred state statute",
    "CICA": "Citizen-initiated constitutional amendment", "CISS": "Citizen-initiated state statute",
    "CICA/SS": "Citizen-initiated constitutional amendment and statute", "IndISS": "Indirect initiated state statute",
    "IndICA": "Indirect initiated constitutional amendment", "BI": "Bond issue", "VR": "Veto referendum",
    "LRAQ": "Legislatively referred advisory question", "CCQ": "Constitutional convention question",
}


def get(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def cells(r):
    return r.find_all(["td", "th"])


def txt(c):
    return re.sub(r"\s*\[[^\]]*\]", "", c.get_text(" ", strip=True)).strip()


def nov3_table(soup):
    """The Ballotpedia deadline tables come one per election date; pick the one that is all Nov. 3, 2026."""
    for tb in soup.find_all("table"):
        rs = tb.find_all("tr")
        if len(rs) < 50:
            continue
        hdr = [txt(c) for c in cells(rs[0])]
        if "Election date" not in hdr:
            continue
        dates = {txt(cells(r)[1]) for r in rs[1:] if len(cells(r)) > 1}
        if dates == {ELECTION_DAY}:
            return hdr, rs[1:]
    raise RuntimeError("Nov. 3 table not found")


def main():
    out = {"meta": {"election_day": "2026-11-03", "retrieved": datetime.date.today().isoformat(),
                    "sources": {
                        "ballotpedia": BP, "wikipedia_legislatures": "https://en.wikipedia.org/wiki/2026_United_States_state_legislative_elections",
                        "wikipedia_governors": "https://en.wikipedia.org/wiki/2026_United_States_gubernatorial_elections"},
                    "measure_types": MEASURE_TYPES}, "states": {}}
    S = out["states"]
    for name, ab in STATES.items():
        S[ab] = {"name": name, "ballotpedia": f"{BP}{name.replace(' ', '_')}_elections,_2026",
                 "governor": ab in GOVERNOR_STATES, "lookup": LOOKUP_URLS[ab], "measures": []}

    # poll hours + notes
    soup = get(BP + "State_Poll_Opening_and_Closing_Times_(2026)")
    tb = [t for t in soup.find_all("table") if len(t.find_all("tr")) > 40][0]
    for r in tb.find_all("tr")[2:]:
        c = cells(r)
        if len(c) >= 4 and txt(c[0]) in STATES:
            s = S[STATES[txt(c[0])]]
            s["hours"], s["hours_note"] = txt(c[1]), txt(c[2])
            s["hours_src"] = BP + "State_Poll_Opening_and_Closing_Times_(2026)"

    # early voting + deadlines
    for key, page in (("early", "Early_voting_dates,_2026"), ("register", "Voter_registration_deadlines,_2026"),
                      ("mail_request", "Absentee/mail-in_ballot_request_deadlines,_2026"),
                      ("mail_return", "Absentee/mail-in_ballot_return_deadlines,_2026")):
        hdr, rows = nov3_table(get(BP + page))
        for r in rows:
            c = cells(r)
            d = dict(zip(hdr, [txt(x) for x in c]))
            a = c[-1].find("a")
            src = a["href"] if a and a.get("href", "").startswith("http") else None
            ab = STATES[d["State"]]
            vals = {h: v for h, v in d.items() if h not in ("State", "Election date", "Source") and v}
            S[ab][key] = {"values": vals, "source": src or BP + page}

    # statewide ballot measures (Ballotpedia's certified list for Nov. 3)
    soup = get(BP + "2026_ballot_measures")
    for tb in soup.find_all("table"):
        rs = tb.find_all("tr")
        hdr = [txt(c) for c in cells(rs[0])] if rs else []
        if hdr == ["Jurisdiction", "Type", "Title", "Description"] and len(rs) > 100:
            for r in rs[1:]:
                c = cells(r)
                jur = txt(c[0])
                if jur not in STATES:
                    continue
                link = [a["href"] for a in c[2].find_all("a") if a.get("href", "").startswith("https://ballotpedia.org/")]
                S[STATES[jur]]["measures"].append({"type": txt(c[1]), "title": txt(c[2]), "summary": txt(c[3]),
                                                   "url": link[0] if link else None})
            break
    else:
        raise RuntimeError("statewide measures table not found")

    # state legislative seats up
    soup = get(out["meta"]["sources"]["wikipedia_legislatures"])
    for tb in soup.find_all("table"):
        rs = tb.find_all("tr")
        if rs and len(rs) > 40 and txt(cells(rs[0])[0]) == "State" and "Upper house" in txt(cells(rs[0])[1]):
            for r in rs[2:]:
                c = [txt(x) for x in cells(r)]
                if not c or c[0] not in STATES:
                    continue
                ab = STATES[c[0]]
                if len(c) == 9:
                    S[ab]["legislature"] = {"upper": {"up": int(c[1]), "total": int(c[2]), "term": c[4]},
                                            "lower": {"up": int(c[5]), "total": int(c[6]), "term": c[8]}}
                elif c[0] == "Nebraska":   # unicameral
                    S[ab]["legislature"] = {"unicameral": {"up": int(c[1].split()[0]), "total": int(c[2].split()[0]), "term": c[4]}}
            break
    missing = [k for k, v in S.items() if k != "DC" and "legislature" not in v]
    assert not missing, missing
    for k in S:
        for need in ("hours", "early", "register", "mail_request", "mail_return"):
            assert need in S[k], (k, need)
    path = os.path.join(ROOT, "data", "elections_2026.json")
    json.dump(out, open(path, "w"), indent=1, ensure_ascii=False)
    print("wrote", path, sum(len(v["measures"]) for v in S.values()), "measures")


if __name__ == "__main__":
    main()

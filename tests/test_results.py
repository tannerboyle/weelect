"""Post-election features: date switch for the Official results box, certified-outcome stamp, officials-elect hook, /results.
Run: python -m unittest discover -s tests
Fake outcomes below are TEST DATA ONLY and are injected in memory; nothing is written to data/."""
import copy, datetime, json, os, re, unittest
from unittest import mock

import app as app_mod
import measures as ms_mod
import results as rs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, EDAY, AFTER = datetime.date(2026, 11, 2), datetime.date(2026, 11, 3), datetime.date(2026, 11, 4)
FAKE = {"outcome": "passed", "yes_pct": 54.2, "no_pct": 45.8, "certified_date": "2026-12-05",
        "source_url": "https://example.gov/TEST-DATA-NOT-REAL"}


def first_measure():
    return ms_mod.items()[0]


class DateSwitch(unittest.TestCase):
    def test_phase(self):
        self.assertEqual(rs.phase(datetime.date(2026, 10, 29)), "before")
        self.assertEqual(rs.phase(BEFORE), "before")
        self.assertEqual(rs.phase(EDAY), "election_day")
        self.assertEqual(rs.phase(AFTER), "after")
        self.assertEqual(rs.phase(datetime.date(2027, 1, 20)), "after")

    def test_box_wording(self):
        b = rs.box("ca", BEFORE)
        self.assertEqual(b["text"], "Results will appear on California\u2019s official site after polls close on Nov. 3.")
        self.assertTrue(rs.box("CA", EDAY)["text"].endswith("tonight."))
        a = rs.box("TX", AFTER)
        self.assertIn("official site as counties report", a["text"])
        self.assertIn("unofficial until the state certifies", a["unofficial"])
        self.assertEqual(rs.box("DC", BEFORE)["text"][:47], "Results will appear on the District of Columbia")
        self.assertIsNone(rs.box("PR", AFTER))
        self.assertIsNone(rs.box("", AFTER))

    def test_today_uses_site_clock(self):
        with mock.patch("elections._today", return_value=AFTER):
            self.assertEqual(rs.phase(), "after")


class LinksData(unittest.TestCase):
    def test_all_states_and_dc(self):
        d = json.load(open(os.path.join(ROOT, "data", "official_results.json")))
        self.assertEqual(len(d["states"]), 51)
        self.assertEqual(set(d["states"]), set(rs.STATE_NAMES))
        for code, s in d["states"].items():
            self.assertTrue(s["url"].startswith("https://"), code)
            self.assertIn(s["status"], ("verified", "flagged"), code)
            self.assertTrue(s["agency"], code)
            if s["status"] == "flagged":
                self.assertTrue(s.get("note"), code)


class Stamp(unittest.TestCase):
    def test_outcome_requires_certified_sourced_data(self):
        it = first_measure()
        self.assertIsNone(rs.outcome(it))  # real data is empty until certification
        self.assertIsNone(rs.outcome(dict(it, outcome="passed")))
        self.assertIsNone(rs.outcome(dict(it, outcome="passed", certified_date="2026-12-05")))
        self.assertIsNone(rs.outcome(dict(it, **dict(FAKE, outcome="won"))))
        self.assertIsNone(rs.outcome(dict(it, **dict(FAKE, source_url="http://insecure.example"))))
        self.assertIsNone(rs.outcome(dict(it, **dict(FAKE, certified_date="soon"))))
        o = rs.outcome(dict(it, **FAKE))
        self.assertEqual((o["label"], o["yes_pct"], o["certified_label"]), ("Passed", 54.2, "Dec. 5, 2026"))
        o = rs.outcome(dict(it, **dict(FAKE, outcome="Failed", yes_pct=150)))
        self.assertEqual(o["label"], "Failed")
        self.assertIsNone(o["yes_pct"])

    def test_ap_dates(self):
        self.assertEqual(rs.ap_date(datetime.date(2027, 1, 3)), "Jan. 3, 2027")
        self.assertEqual(rs.ap_date(datetime.date(2027, 3, 2)), "March 2, 2027")
        self.assertEqual(rs.ap_date(datetime.date(2026, 9, 9)), "Sept. 9, 2026")


class Pages(unittest.TestCase):
    def setUp(self):
        self.c = app_mod.app.test_client()

    def get(self, path, day):
        with mock.patch("results.today", return_value=day), mock.patch("elections._today", return_value=day):
            r = self.c.get(path)
        return r, r.get_data(as_text=True)

    def test_measure_page_box_switches_by_date(self):
        it = first_measure()
        r, html = self.get(f"/measures/{it['id']}", BEFORE)
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="offres"', html)
        self.assertIn("after polls close on Nov. 3", html)
        self.assertIn(rs.links()["states"][it["state"]]["url"], html)
        self.assertNotIn("ms-stamp", html)  # no outcome yet: no stamp
        r, html = self.get(f"/measures/{it['id']}", AFTER)
        self.assertIn("as counties report them", html)
        self.assertIn("Counts are unofficial until the state certifies them.", html)
        self.assertNotIn("after polls close on Nov. 3", html)

    def test_stamp_renders_with_fake_data(self):
        it = first_measure()
        fake = dict(copy.deepcopy(it), **FAKE)
        with mock.patch.object(ms_mod, "get", return_value=fake), mock.patch.object(ms_mod, "items", return_value=[fake]):
            r, html = self.get(f"/measures/{it['id']}", AFTER)
        self.assertEqual(r.status_code, 200)
        self.assertIn('aria-label="Certified result: Passed"', html)
        self.assertIn("Yes 54.2% &middot; No 45.8%", html)
        self.assertIn("Certified Dec. 5, 2026", html)
        self.assertIn(FAKE["source_url"], html)

    def test_results_route_hidden_until_data(self):
        r, _ = self.get("/results", AFTER)
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("/results<", self.c.get("/sitemap.xml").get_data(as_text=True))
        _, idx = self.get("/measures", AFTER)
        self.assertNotIn('href="/results"', idx)
        it = first_measure()
        fake = dict(copy.deepcopy(it), **dict(FAKE, outcome="failed"))
        with mock.patch.object(ms_mod, "items", return_value=[fake]):
            r, html = self.get("/results", AFTER)
            sm = self.c.get("/sitemap.xml").get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("0 passed · 1 failed", html)
        self.assertIn("Certified result: Failed", html)
        self.assertIn("/results</loc>", sm)


class OfficialsElect(unittest.TestCase):
    ENTRY = {"kind": "fed", "id": "X000001", "seat": "U.S. House, Test district", "elected_name": "Test Person",
             "takes_office": "2027-01-03", "certified_date": "2026-12-05", "source_url": "https://example.gov/TEST-DATA-NOT-REAL"}

    def entries(self, *es):
        return mock.patch("results._load", side_effect=lambda p: {"entries": list(es)} if p == rs.ELECT_PATH else json.load(open(p)))

    def test_empty_by_default(self):
        self.assertEqual(rs.elect_entries(), [])
        self.assertIsNone(rs.elect_note("fed", "X000001"))

    def test_note_wording_and_expiry(self):
        with self.entries(self.ENTRY), mock.patch("results.today", return_value=AFTER):
            n = rs.elect_note("fed", "X000001", "Someone Else")
            self.assertEqual(n["text"], "Test Person was elected to this seat on Nov. 3, 2026 and takes office Jan. 3, 2027.")
            self.assertTrue(rs.elect_note("fed", "X000001", "test person")["reelected"])
        with self.entries(self.ENTRY), mock.patch("results.today", return_value=datetime.date(2027, 1, 3)):
            self.assertIsNone(rs.elect_note("fed", "X000001"))

    def test_incomplete_entry_ignored(self):
        bad = dict(self.ENTRY); bad.pop("source_url")
        with self.entries(bad):
            self.assertEqual(rs.elect_entries(), [])


if __name__ == "__main__":
    unittest.main()

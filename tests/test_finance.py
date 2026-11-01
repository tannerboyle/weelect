"""'Follow the money' on ballot measure pages: data integrity, the donor display rule, and neutral rendering.
Run: python -m unittest discover -s tests"""
import json, os, re, unittest
from unittest import mock

import app as app_mod
import finance as ft
import measures as ms_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = json.load(open(os.path.join(ROOT, "data", "measure_finance.json")))


def d(name, amount):
    return {"name": name, "amount": amount}


class Data(unittest.TestCase):
    def test_every_measure_has_an_entry(self):
        self.assertEqual(set(DOC["items"]), {i["id"] for i in ms_mod.items()})

    def test_shape_and_sources(self):
        for slug, it in DOC["items"].items():
            self.assertTrue(it["source"]["url"].startswith("https://ballotpedia.org/"), slug)
            self.assertTrue(it["filings"]["url"].startswith("https://"), slug)
            if it["as_of"]:
                self.assertRegex(it["as_of"], r"^2026-\d\d-\d\d$", slug)
            for side in ("yes", "no"):
                s = it[side]
                for c in s["committees"]:
                    self.assertTrue(c["name"].strip(), slug)
                    self.assertGreaterEqual(c["total"], 0, slug)
                for x in s["donors"] + s["ie"]:
                    self.assertTrue(x["name"].strip(), slug)
                    self.assertGreater(x["amount"], 0, slug)
                if not s["committees"]:
                    self.assertFalse(s["donors"], f"{slug} {side}: donors without a committee")

    def test_spot_checks_against_source(self):
        # Copied by hand from the Ballotpedia pages read on 2026-11-01.
        ca39 = DOC["items"]["california-proposition-39"]
        self.assertEqual(ca39["yes"]["total"], 39540774.87)
        self.assertEqual(ca39["no"]["total"], 33277742.21)
        self.assertEqual(ca39["yes"]["donors"][0], d("Richard Uihlein", 17000000.0))
        self.assertEqual(ca39["as_of"], "2026-10-22")
        self.assertEqual(DOC["items"]["florida-amendment-3"]["yes"]["donors"][0], d("Florida Realtors", 19500000.0))
        self.assertEqual(DOC["items"]["missouri-amendment-3"]["no"]["donors"][0], d("The Fairness Project", 11945000.0))
        self.assertEqual(DOC["items"]["nevada-question-6"]["yes"]["total"], 1568861.29)  # 2026, not the 2024 table
        self.assertEqual(DOC["items"]["ohio-issue-3"]["yes"]["committees"], [])


class Rule(unittest.TestCase):
    def test_threshold(self):
        shown, rule = ft.pick([d("a", 50_000), d("b", 100_000), d("c", 250_000), d("e", 120_000)])
        self.assertEqual(rule, "threshold")
        self.assertEqual([x["name"] for x in shown], ["c", "e", "b"])

    def test_fallback_top_five_with_floor(self):
        shown, rule = ft.pick([d("a", 150_000), d("b", 40_000), d("c", 9_000), d("e", 5_000), d("f", 4_999), d("g", 3_000)])
        self.assertEqual(rule, "fallback")
        self.assertEqual([x["name"] for x in shown], ["a", "b", "c", "e"])  # f is in the top five but under the floor

    def test_cap(self):
        shown, _ = ft.pick([d(str(i), 200_000 + i) for i in range(15)])
        self.assertEqual(len(shown), ft.MAX_SHOWN)

    def test_states_and_share(self):
        self.assertEqual(ft.get("ohio-issue-3")["yes"]["state"], "none")
        self.assertEqual(ft.get("michigan-proposal-1")["yes"]["state"], "zero")
        fl = ft.get("florida-amendment-3")["yes"]
        self.assertEqual(fl["state"], "money")
        self.assertEqual(fl["share"], 100)  # 19.5M + 2.0165M of 21,516,510.33
        # Multi-measure committees: no share, and the other measures are named.
        ca = ft.get("california-proposition-41", {"california-proposition-40": "California Proposition 40"})["yes"]
        self.assertIsNone(ca["share"])
        self.assertIn("California Proposition 40", ca["shared_with"])

    def test_money(self):
        self.assertEqual(ft.money(74864711.10), "$74,864,711")
        self.assertEqual(ft.money(916.32), "$916")
        self.assertEqual(ft.money(None), "—")
        self.assertEqual(ft.as_of_label("2026-10-22"), "Oct. 22, 2026")


class Page(unittest.TestCase):
    def setUp(self):
        self.c = app_mod.app.test_client()

    def section(self, slug):
        r = self.c.get(f"/measures/{slug}")
        self.assertEqual(r.status_code, 200)
        h = r.get_data(as_text=True)
        i = h.find('<section class="ftm"')
        self.assertGreater(i, 0, slug)
        return h[i:h.find('<div class="bs-sources"', i)]

    def test_every_measure_page_renders_section(self):
        for it in ms_mod.items():
            s = self.section(it["id"])
            self.assertIn("Supporting (Yes)", s)
            self.assertIn("Opposing (No)", s)
            self.assertIn("may be updated", s)
            self.assertIn("Official filings", s)
            self.assertEqual(s.count('class="ftm-side"'), 2)

    def test_heavy_spending_page(self):
        s = self.section("california-proposition-39")
        self.assertIn("$39,540,775", s)
        self.assertIn("Richard Uihlein", s)
        self.assertIn("through Oct. 22, 2026", s)
        self.assertIn("cal-access.sos.ca.gov", s)
        self.assertNotIn("Who is paying for the campaigns", self.c.get("/measures/california-proposition-39").get_data(as_text=True))

    def test_empty_states(self):
        s = self.section("ohio-issue-3")
        self.assertEqual(s.count("No committee registered."), 2)
        s = self.section("north-carolina-photo-id-amendment")
        self.assertIn("No major donors reported.", s)  # committee with no donor table

    def test_neutral_wording(self):
        loaded = re.compile(r"\b(dark money|special interests?|shady|massive|huge|mega|out-of-state|billionaires?|corporate)\b", re.I)
        for it in ms_mod.items():
            s = re.sub(r"<[^>]+>", " ", self.section(it["id"]))
            # Committee and donor names are quoted from filings and may contain any word; check our own text only.
            names = [c["name"] for side in ("yes", "no") for c in DOC["items"][it["id"]][side]["committees"]]
            names += [x["name"] for side in ("yes", "no") for x in DOC["items"][it["id"]][side]["donors"] + DOC["items"][it["id"]][side]["ie"]]
            for n in names:
                s = s.replace(n.replace("&", "&amp;"), " ").replace(n, " ")
            self.assertIsNone(loaded.search(s), (it["id"], loaded.search(s)))

    def test_falls_back_to_old_paragraph_without_data(self):
        with mock.patch.object(ft, "get", return_value=None):
            h = self.c.get("/measures/california-proposition-39").get_data(as_text=True)
        self.assertNotIn('<section class="ftm"', h)
        self.assertIn("Who is paying for the campaigns", h)


if __name__ == "__main__":
    unittest.main()

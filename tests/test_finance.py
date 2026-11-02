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
            if it["source"].get("kind") == "official":
                self.assertRegex(it["source"]["url"], r"^https://(data\.wa\.gov|powersearch\.sos\.ca\.gov)/", slug)
            else:
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


class Official(unittest.TestCase):
    """Washington from PDC open data, California 40-42 from CAL-ACCESS (Power Search), read on 2026-11-01."""
    def test_washington_from_pdc(self):
        i645 = DOC["items"]["washington-initiative-645"]
        self.assertEqual(i645["source"]["kind"], "official")
        self.assertIn("data.wa.gov", i645["source"]["url"])
        self.assertEqual(i645["as_of"], "2026-10-30")
        self.assertEqual(i645["yes"]["total"], 11948200.77)
        # 13,950,001.92 across the four committees less the 207,000 No on 645 gave Defend Washington.
        self.assertEqual(i645["no"]["total"], 13743001.92)
        self.assertIn("$207,000", i645["no"]["total_note"])
        i1 = DOC["items"]["washington-initiative-1"]
        self.assertEqual((i1["yes"]["total"], i1["no"]["total"]), (8258315.77, 5289999.77))
        i638 = DOC["items"]["washington-initiative-638"]
        self.assertEqual((i638["no"]["total"], i638["no"]["donors"]), (i1["no"]["total"], i1["no"]["donors"]))

    def test_washington_merges_only_same_address(self):
        names = [x["name"] for x in DOC["items"]["washington-initiative-645"]["yes"]["donors"]]
        self.assertEqual(names.count("Hughes Lawrence"), 2)   # 8865 Overlake Dr vs 8865 Overlake Dr W: not combined
        self.assertEqual(names.count("Gordon Larry"), 2)      # 21309 vs 2130 Snag Island Dr: not combined
        self.assertEqual(names.count("Crow Robert"), 1)       # "#713" and "Unit 713": same address
        s = ft.get("washington-initiative-645")["yes"]
        self.assertTrue(s["same_name"])

    def test_california_from_cal_access(self):
        for slug in ("california-proposition-40", "california-proposition-41", "california-proposition-42"):
            it = DOC["items"][slug]
            self.assertEqual(it["source"]["kind"], "official", slug)
            self.assertEqual(it["as_of"], "2026-10-31", slug)
            for side in ("yes", "no"):
                s = it[side]
                self.assertLessEqual(sum(x["amount"] for x in s["donors"]), s["total"] + 0.01, (slug, side))
                self.assertAlmostEqual(sum(c["total"] for c in s["committees"]), s["total"], 2)
        p41 = DOC["items"]["california-proposition-41"]
        self.assertEqual(p41["yes"]["total"], 58376445.92)
        self.assertEqual([c["name"] for c in p41["yes"]["committees"]], ["Yes on 41 - Californians for Transparency and Accountability"])
        self.assertEqual(p41["yes"]["donors"][0]["name"], "Building a Better California")
        self.assertEqual(p41["no"]["total"], 34565577.01)
        self.assertEqual(DOC["items"]["california-proposition-42"]["yes"]["total"], 159277037.26)
        # Building a Better California gives to committees; it is not itself a Prop 40-42 committee in CAL-ACCESS.
        self.assertNotIn("Building a Better California", [c["name"] for c in DOC["items"]["california-proposition-40"]["no"]["committees"]])

    def test_donor_list_never_exceeds_total_without_shared_note(self):
        raw = {"committees": [{"name": "C", "total": 100.0}], "total": 100.0, "donors": [d("x", 90_000), d("y", 80_000), d("z", 70_000)]}
        s = ft._side(raw, "x", {})
        self.assertEqual(s["donors"], [])
        self.assertEqual(s["donor_note"], "Donor details are being verified.")
        raw["committees"][0]["also"] = ["other"]
        self.assertEqual(len(ft._side(raw, "x", {})["donors"]), 3)

    def test_donors_hidden_flag(self):
        raw = {"committees": [{"name": "C", "total": 1e6}], "total": 1e6, "donors": [d("x", 200_000)], "donors_hidden": True}
        s = ft._side(raw, "x", {})
        self.assertEqual((s["donors"], s["donor_note"]), ([], "Donor details are being verified."))

    def test_official_pages(self):
        c = app_mod.app.test_client()
        for slug, needles in {"washington-initiative-645": ["Washington Public Disclosure Commission", "through Oct. 30, 2026", "$207,000", "$11,948,201"],
                              "california-proposition-41": ["CAL-ACCESS", "through Oct. 31, 2026", "Building a Better California", "$58,376,446",
                                                            "Linked donors file their own reports"]}.items():
            h = c.get(f"/measures/{slug}").get_data(as_text=True)
            for n in needles:
                self.assertIn(n, h.replace("&#39;", "'"), (slug, n))
            self.assertNotIn("compiled by", h[h.find('<section class="ftm"'):])


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
        ca = ft.get("california-proposition-41", {"california-proposition-40": "California Proposition 40"})["no"]
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

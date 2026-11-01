"""Newsletter signup (Resend). Resend is always mocked: no network calls, no real email addresses.
Run: python -m unittest discover -s tests"""
import os, time, unittest
from unittest import mock

import app as app_mod
import newsletter as nl

ENV = {"RESEND_API_KEY": "re_test_not_real", "RESEND_SEGMENT_ID": "seg_test", "SECRET_KEY": "test-secret",
       "NEWSLETTER_FROM": "", "RESEND_TOPIC_ID": "", "RESEND_AUDIENCE_ID": "", "NEWSLETTER_DAILY_CAP": "80"}
ORIGIN = {"Origin": "http://localhost"}


class Base(unittest.TestCase):
    env = ENV

    def setUp(self):
        self.envp = mock.patch.dict(os.environ, self.env)
        self.envp.start()
        app_mod.app.testing = True
        self.c = app_mod.app.test_client()
        app_mod._HITS.clear(); nl._RECENT.clear(); nl._DAY.update(date=None, count=0)
        self.api = mock.patch("newsletter._resend", return_value={"id": "x"})
        self.resend = self.api.start()

    def tearDown(self):
        self.api.stop(); self.envp.stop()

    def subscribe(self, email="reader@example.com", headers=ORIGIN, **extra):
        with app_mod.app.test_request_context():
            tok = nl.form_token()
        data = {"email": email, "csrf": tok, "website": ""}
        data.update(extra)
        return self.c.post("/newsletter/subscribe", data=data, headers=headers)

    def sent_link(self):
        body = self.resend.call_args.args[2]
        self.assertIn("/newsletter/confirm?t=", body["html"])
        return body["text"].split("https://weelect.org", 1)[1].split()[0]


class Signup(Base):
    def test_home_and_get_involved_show_form(self):
        for path in ("/", "/get-involved", "/newsletter"):
            html = self.c.get(path).get_data(as_text=True)
            self.assertIn('action="/newsletter/subscribe"', html, path)
            self.assertIn('name="csrf"', html, path)
            self.assertIn("unsubscribe anytime", html, path)

    def test_signup_sends_confirmation_only(self):
        r = self.subscribe("Reader@Example.COM")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Check your email", r.get_data(as_text=True))
        self.assertEqual(self.resend.call_count, 1)
        method, path, body = self.resend.call_args.args
        self.assertEqual((method, path), ("POST", "/emails"))            # an email, not a contact
        self.assertEqual(body["to"], ["Reader@example.com"])                # domain lower-cased, local part kept
        self.assertEqual(body["from"], "We Elect <updates@news.weelect.org>")
        self.assertIn("Confirm my email", body["html"])
        self.assertTrue(r.headers["Cache-Control"] == "no-store")

    def test_invalid_emails(self):
        for bad in ("", "not-an-email", "a@b", "a..b@example.com", "x@exa mple.com", "<a@example.com>",
                    "a" * 65 + "@example.com", "a@" + "b" * 250 + ".com"):
            app_mod._HITS.clear()                        # invalid tries count toward the rate limit too
            r = self.subscribe(bad)
            self.assertEqual(r.status_code, 400, bad)
        self.resend.assert_not_called()

    def test_honeypot_pretends_success_and_sends_nothing(self):
        r = self.subscribe(website="http://spam.example")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Check your email", r.get_data(as_text=True))
        self.resend.assert_not_called()

    def test_csrf_requires_origin_and_form_token(self):
        self.assertEqual(self.subscribe(headers={"Origin": "https://evil.example"}).status_code, 400)
        self.assertEqual(self.subscribe(headers={}).status_code, 400)                       # no Origin, no Referer
        self.assertEqual(self.subscribe(csrf="forged").status_code, 400)
        self.assertEqual(self.subscribe(headers={"Referer": "http://localhost/get-involved"}).status_code, 200)
        self.assertEqual(self.resend.call_count, 1)

    def test_form_token_expires(self):
        with app_mod.app.test_request_context():
            tok = nl.form_token()
        with mock.patch("itsdangerous.timed.time.time", return_value=time.time() + nl.FORM_MAX_AGE + 5):
            self.assertFalse(nl.form_token_ok(tok))
        self.assertTrue(nl.form_token_ok(tok))

    def test_rate_limit_per_ip(self):
        codes = [self.subscribe(f"reader{i}@example.com").status_code for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])
        self.assertEqual(self.resend.call_count, 5)

    def test_same_address_cooldown(self):
        self.subscribe("same@example.com")
        app_mod._HITS.clear()
        r = self.subscribe("same@example.com")
        self.assertEqual(r.status_code, 200)                 # same page, no second email
        self.assertEqual(self.resend.call_count, 1)

    def test_daily_cap(self):
        with mock.patch.dict(os.environ, {"NEWSLETTER_DAILY_CAP": "2"}):
            codes = []
            for i in range(3):
                app_mod._HITS.clear()
                codes.append(self.subscribe(f"cap{i}@example.com").status_code)
        self.assertEqual(codes, [200, 200, 503])

    def test_resend_failure_is_friendly(self):
        self.resend.side_effect = nl.ResendError("POST emails -> 500")
        r = self.subscribe()
        self.assertEqual(r.status_code, 502)
        self.assertIn("our problem, not yours", r.get_data(as_text=True))


class Confirm(Base):
    def test_get_shows_button_and_does_not_subscribe(self):
        self.subscribe("reader@example.com")
        link = self.sent_link()
        self.resend.reset_mock()
        r = self.c.get(link)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Yes, sign me up", html)
        self.assertIn("reader@example.com", html)
        self.resend.assert_not_called()                      # link scanners can't subscribe anyone

    def test_valid_token_creates_contact_in_segment(self):
        self.subscribe("reader@example.com")
        tok = self.sent_link().split("t=", 1)[1]
        self.resend.reset_mock()
        r = self.c.post("/newsletter/confirm", data={"t": tok}, headers=ORIGIN)
        self.assertEqual(r.status_code, 200)
        self.assertIn("you&rsquo;re subscribed", r.get_data(as_text=True))
        method, path, body = self.resend.call_args.args
        self.assertEqual((method, path), ("POST", "/contacts"))
        self.assertEqual(body, {"email": "reader@example.com", "unsubscribed": False, "segments": [{"id": "seg_test"}]})

    def test_existing_contact_is_resubscribed(self):
        with app_mod.app.test_request_context():
            tok = nl.make_confirm_token("back@example.com")
        self.resend.side_effect = [nl.ResendError("POST contacts -> 409"), {}, {}]
        r = self.c.post("/newsletter/confirm", data={"t": tok}, headers=ORIGIN)
        self.assertEqual(r.status_code, 200)
        calls = [c.args[:2] for c in self.resend.call_args_list]
        self.assertEqual(calls, [("POST", "/contacts"), ("PATCH", "/contacts/back@example.com"),
                                 ("POST", "/contacts/back@example.com/segments/seg_test")])
        self.assertEqual(self.resend.call_args_list[1].args[2], {"unsubscribed": False})

    def test_topic_opt_in_when_configured(self):
        with mock.patch.dict(os.environ, {"RESEND_TOPIC_ID": "topic_test"}):
            with app_mod.app.test_request_context():
                tok = nl.make_confirm_token("t@example.com")
            self.c.post("/newsletter/confirm", data={"t": tok}, headers=ORIGIN)
        self.assertEqual(self.resend.call_args.args[2]["topics"], [{"id": "topic_test", "subscription": "opt_in"}])

    def test_expired_token(self):
        with app_mod.app.test_request_context():
            tok = nl.make_confirm_token("late@example.com")
        with mock.patch("itsdangerous.timed.time.time", return_value=time.time() + nl.CONFIRM_MAX_AGE + 5):
            r = self.c.post("/newsletter/confirm", data={"t": tok}, headers=ORIGIN)
            g = self.c.get("/newsletter/confirm?t=" + tok)
        self.assertEqual(r.status_code, 400)
        self.assertIn("That link has expired", r.get_data(as_text=True))
        self.assertEqual(g.status_code, 400)
        self.resend.assert_not_called()

    def test_tampered_token(self):
        with app_mod.app.test_request_context():
            tok = nl.make_confirm_token("me@example.com")
        payload, rest = tok.split(".", 1)
        forged = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB") + "." + rest
        for t in (forged, tok + "x", "", "garbage"):
            r = self.c.post("/newsletter/confirm", data={"t": t}, headers=ORIGIN)
            self.assertEqual(r.status_code, 400, t)
            self.assertIn("That link didn&rsquo;t work", r.get_data(as_text=True))
        with mock.patch.dict(os.environ, {"SECRET_KEY": "a-different-secret"}):   # signed with another key
            self.assertEqual(self.c.post("/newsletter/confirm", data={"t": tok}, headers=ORIGIN).status_code, 400)
        self.resend.assert_not_called()

    def test_confirm_post_needs_same_origin(self):
        with app_mod.app.test_request_context():
            tok = nl.make_confirm_token("me@example.com")
        r = self.c.post("/newsletter/confirm", data={"t": tok}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 400)
        self.resend.assert_not_called()

    def test_form_token_cannot_be_used_as_confirm_token(self):
        with app_mod.app.test_request_context():
            ftok = nl.form_token()
        self.assertEqual(self.c.post("/newsletter/confirm", data={"t": ftok}, headers=ORIGIN).status_code, 400)


class ComingSoon(Base):
    env = dict(ENV, RESEND_API_KEY="", RESEND_SEGMENT_ID="")

    def test_pages_show_coming_soon(self):
        for path in ("/", "/get-involved", "/newsletter"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            html = r.get_data(as_text=True)
            self.assertIn("Newsletter coming soon", html, path)
            self.assertNotIn('action="/newsletter/subscribe"', html, path)

    def test_posts_do_not_error_or_call_resend(self):
        r = self.c.post("/newsletter/subscribe", data={"email": "a@example.com"}, headers=ORIGIN)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Newsletter coming soon", r.get_data(as_text=True))
        self.assertEqual(self.c.get("/newsletter/confirm?t=x").status_code, 200)
        self.resend.assert_not_called()

    def test_segment_alone_is_not_enough(self):
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_x"}):
            self.assertFalse(nl.enabled())
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_x", "RESEND_AUDIENCE_ID": "aud_legacy"}):
            self.assertTrue(nl.enabled())          # old "audience" id accepted as an alias

    def test_sitemap_and_robots(self):
        self.assertNotIn("/newsletter<", self.c.get("/sitemap.xml").get_data(as_text=True))
        self.assertIn("Disallow: /newsletter/", self.c.get("/robots.txt").get_data(as_text=True))


class RestClient(unittest.TestCase):
    def test_request_shape_and_errors(self):
        with mock.patch.dict(os.environ, ENV), mock.patch("newsletter.requests.request") as req:
            req.return_value = mock.Mock(status_code=200, content=b'{"id":"e1"}', json=lambda: {"id": "e1"})
            self.assertEqual(nl._resend("POST", "/emails", {"a": 1}), {"id": "e1"})
            kw = req.call_args.kwargs
            self.assertEqual(req.call_args.args, ("POST", "https://api.resend.com/emails"))
            self.assertEqual(kw["headers"]["Authorization"], "Bearer re_test_not_real")
            self.assertIn("User-Agent", kw["headers"])            # Resend rejects requests without one
            req.return_value = mock.Mock(status_code=422, content=b"{}", json=lambda: {"name": "validation_error"})
            with self.assertRaises(nl.ResendError) as e:
                nl._resend("POST", "/contacts", {})
            self.assertNotIn("re_test", str(e.exception))


if __name__ == "__main__":
    unittest.main()

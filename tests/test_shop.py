"""Shop tests. No network and no real keys: Stripe and Printful HTTP calls are mocked.
Run:  python -m unittest discover -s tests -v
"""
import hashlib, hmac, json, os, re, tempfile, time, unittest
from unittest import mock

TEST_ENV = {"STRIPE_SECRET_KEY": "sk_test_unit", "STRIPE_PUBLISHABLE_KEY": "pk_test_unit", "STRIPE_WEBHOOK_SECRET": "whsec_unit",
            "PRINTFUL_API_TOKEN": "pf_unit", "SITE_URL": "https://weelect.org"}
SESSION_ID = "cs_test_a1B2c3D4e5F6g7H8i9J0"


def sign(payload: bytes, secret="whsec_unit", ts=None):
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body if body is not None else {}

    def json(self):
        return self._b


def completed_event(sid=SESSION_ID, cart="tee.navy.M:2,hat.navy.One size:1", paid="paid", subtotal=8400, extra=None):
    sess = {"id": sid, "object": "checkout.session", "payment_status": paid, "amount_subtotal": subtotal, "amount_total": 9300,
            "total_details": {"amount_shipping": 795, "amount_tax": 105},
            "metadata": {"source": "weelect-shop", "cart": cart},
            "customer_details": {"email": "reader@example.com", "name": "Pat Reader"},
            "shipping_details": {"name": "Pat Reader", "address": {"line1": "1 Main St", "line2": None, "city": "Austin", "state": "TX", "postal_code": "78701", "country": "US"}}}
    sess.update(extra or {})
    return {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": sess}}


class ShopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = dict(TEST_ENV, SHOP_ORDER_LOG=os.path.join(self.tmp, "orders.jsonl"))
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        import app as appmod
        self.app = appmod.app
        self.c = self.app.test_client()
        self.base = "https://weelect.org"
        appmod._HITS.clear()

    def tearDown(self):
        self.patch.stop()

    def log_records(self):
        p = os.environ["SHOP_ORDER_LOG"]
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f]

    def csrf(self):
        r = self.c.get("/shop/checkout", base_url=self.base)
        self.assertEqual(r.status_code, 200)
        return re.search(r'name="csrf-token" content="([^"]+)"', r.get_data(as_text=True)).group(1)

    def post_webhook(self, event, header=None, raw=None):
        raw = raw if raw is not None else json.dumps(event).encode()
        return self.c.post("/stripe/webhook", data=raw, headers={"Stripe-Signature": header or sign(raw), "Content-Type": "application/json"}, base_url=self.base)


class SignatureTests(unittest.TestCase):
    def test_valid_invalid_stale(self):
        import shop
        body = b'{"a":1}'
        self.assertTrue(shop.verify_stripe_signature(body, sign(body), "whsec_unit"))
        self.assertFalse(shop.verify_stripe_signature(body, sign(body, secret="whsec_other"), "whsec_unit"))
        self.assertFalse(shop.verify_stripe_signature(body + b" ", sign(body), "whsec_unit"), "tampered body")
        self.assertFalse(shop.verify_stripe_signature(body, sign(body, ts=int(time.time()) - 3600), "whsec_unit"), "stale timestamp")
        self.assertFalse(shop.verify_stripe_signature(body, "", "whsec_unit"))
        self.assertFalse(shop.verify_stripe_signature(body, "t=abc,v1=00", "whsec_unit"))
        self.assertFalse(shop.verify_stripe_signature(body, sign(body), ""))

    def test_multiple_v1_signatures(self):
        import shop
        body = b"{}"
        h = sign(body)
        self.assertTrue(shop.verify_stripe_signature(body, h + ",v1=deadbeef", "whsec_unit"))
        self.assertTrue(shop.verify_stripe_signature(body, h.replace(",v1=", ",v1=deadbeef,v1="), "whsec_unit"))


class WebhookTests(ShopBase):
    def test_bad_signature_rejected(self):
        ev = completed_event()
        r = self.post_webhook(ev, header="t=1,v1=00")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(b"Traceback", r.data)

    def test_creates_draft_printful_order_once(self):
        ev = completed_event()
        with mock.patch("shop.requests.post", return_value=Resp(200, {"code": 200, "result": {"id": 99, "status": "draft"}})) as post:
            r = self.post_webhook(ev)
            self.assertEqual(r.status_code, 200, r.data)
            r2 = self.post_webhook(ev)       # Stripe retry / replay
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(post.call_count, 1, "idempotent: Printful is called once")
        args, kw = post.call_args
        self.assertEqual(args[0], "https://api.printful.com/orders", "draft by default: no confirm=true")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer pf_unit")
        payload = json.loads(kw["data"])
        self.assertEqual(payload["external_id"], "we" + hashlib.sha256(SESSION_ID.encode()).hexdigest()[:30])
        self.assertLessEqual(len(payload["external_id"]), 32)
        self.assertEqual(payload["recipient"], {"name": "Pat Reader", "address1": "1 Main St", "address2": "", "city": "Austin", "state_code": "TX",
                                                "country_code": "US", "zip": "78701", "email": "reader@example.com"})
        items = {i["variant_id"]: i for i in payload["items"]}
        self.assertEqual(items[4112]["quantity"], 2, "navy M tee variant")
        self.assertEqual(items[7857]["quantity"], 1, "navy dad hat variant")
        self.assertEqual(items[4112]["retail_price"], "28.00")
        self.assertTrue(items[4112]["files"][0]["url"].startswith("https://weelect.org/static/shop/print/"))
        recs = self.log_records()
        self.assertEqual([r["state"] for r in recs], ["created"])
        self.assertEqual(recs[0]["printful_id"], 99)
        self.assertFalse(recs[0]["confirmed"])

    def test_auto_confirm_flag(self):
        with mock.patch.dict(os.environ, {"PRINTFUL_AUTO_CONFIRM": "1"}), \
             mock.patch("shop.requests.post", return_value=Resp(200, {"result": {"id": 5, "status": "pending"}})) as post:
            self.post_webhook(completed_event())
        self.assertEqual(post.call_args[0][0], "https://api.printful.com/orders?confirm=true")

    def test_subtotal_mismatch_never_auto_confirms(self):
        with mock.patch.dict(os.environ, {"PRINTFUL_AUTO_CONFIRM": "1"}), \
             mock.patch("shop.requests.post", return_value=Resp(200, {"result": {"id": 5, "status": "draft"}})) as post:
            self.post_webhook(completed_event(subtotal=1234))
        self.assertEqual(post.call_args[0][0], "https://api.printful.com/orders")
        self.assertFalse(self.log_records()[0]["subtotal_matches"])

    def test_newer_stripe_shipping_location(self):
        ev = completed_event()
        s = ev["data"]["object"]
        s["collected_information"] = {"shipping_details": s.pop("shipping_details")}
        with mock.patch("shop.requests.post", return_value=Resp(200, {"result": {"id": 7}})) as post:
            self.assertEqual(self.post_webhook(ev).status_code, 200)
        self.assertEqual(json.loads(post.call_args[1]["data"])["recipient"]["city"], "Austin")

    def test_unpaid_foreign_and_other_events_ignored(self):
        with mock.patch("shop.requests.post") as post:
            self.assertEqual(self.post_webhook(completed_event(paid="unpaid")).status_code, 200)
            foreign = completed_event()
            foreign["data"]["object"]["metadata"] = {}
            self.assertEqual(self.post_webhook(foreign).status_code, 200)
            self.assertEqual(self.post_webhook({"id": "e", "type": "charge.refunded", "data": {"object": {}}}).status_code, 200)
            post.assert_not_called()

    def test_printful_server_error_asks_stripe_to_retry(self):
        ev = completed_event()
        with mock.patch("shop.requests.post", return_value=Resp(503, {})) as post:
            self.assertEqual(self.post_webhook(ev).status_code, 500)
        with mock.patch("shop.requests.post", return_value=Resp(200, {"result": {"id": 8, "status": "draft"}})) as post:
            self.assertEqual(self.post_webhook(ev).status_code, 200, "retry succeeds")
            self.assertEqual(post.call_count, 1)

    def test_printful_rejection_is_logged_not_retried(self):
        ev = completed_event()
        with mock.patch("shop.requests.post", return_value=Resp(400, {"error": {"message": "Variant not available"}})) as post:
            self.assertEqual(self.post_webhook(ev).status_code, 200)
            self.assertEqual(self.post_webhook(ev).status_code, 200)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(self.log_records()[0]["state"], "failed_permanent")

    def test_printful_duplicate_external_id_counts_as_done(self):
        with mock.patch("shop.requests.post", return_value=Resp(400, {"error": {"message": "Order with this external ID already exists"}})):
            self.assertEqual(self.post_webhook(completed_event()).status_code, 200)
        self.assertEqual(self.log_records()[0]["state"], "created")

    def test_missing_printful_token_logs_for_manual_order(self):
        with mock.patch.dict(os.environ, {"PRINTFUL_API_TOKEN": ""}), mock.patch("shop.requests.post") as post:
            self.assertEqual(self.post_webhook(completed_event()).status_code, 200)
            post.assert_not_called()
        self.assertEqual(self.log_records()[0]["state"], "failed_permanent")

    def test_tampered_cart_is_not_fulfilled(self):
        with mock.patch("shop.requests.post") as post:
            self.assertEqual(self.post_webhook(completed_event(cart="tee.navy.M:999")).status_code, 200)
            post.assert_not_called()


class CheckoutTests(ShopBase):
    def post(self, items, token="auto", **kw):
        tok = self.csrf() if token == "auto" else token
        headers = {"X-CSRF-Token": tok} if tok else {}
        return self.c.post("/shop/checkout", json={"items": items}, headers=headers, base_url=self.base, **kw)

    def test_requires_csrf(self):
        self.csrf()  # sets cookie
        r = self.c.post("/shop/checkout", json={"items": [{"p": "tee", "c": "navy", "s": "M", "q": 1}]}, base_url=self.base)
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/shop/checkout", json={"items": [{"p": "tee", "c": "navy", "s": "M", "q": 1}]}, headers={"X-CSRF-Token": "x.y.z"}, base_url=self.base)
        self.assertEqual(r.status_code, 403)

    def test_cross_origin_rejected(self):
        tok = self.csrf()
        r = self.c.post("/shop/checkout", json={"items": [{"p": "tee", "c": "navy", "s": "M", "q": 1}]},
                        headers={"X-CSRF-Token": tok, "Origin": "https://evil.example"}, base_url=self.base)
        self.assertEqual(r.status_code, 403)

    def test_expired_token_rejected(self):
        import shop
        tok = self.csrf()
        with mock.patch("shop.time.time", return_value=time.time() + shop.CSRF_TTL + 60):
            r = self.c.post("/shop/checkout", json={"items": [{"p": "tee", "c": "navy", "s": "M", "q": 1}]}, headers={"X-CSRF-Token": tok}, base_url=self.base)
        self.assertEqual(r.status_code, 403)

    def test_creates_embedded_session_with_server_side_prices(self):
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            return Resp(200, {"id": SESSION_ID, "client_secret": "cs_test_secret_abc"})
        with mock.patch("shop.requests.request", side_effect=fake):
            r = self.post([{"p": "tee", "c": "navy", "s": "M", "q": 2, "price": 1, "price_cents": 1, "unit_amount": 1}, {"p": "hat", "c": "navy", "s": "One size", "q": 1}])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json(), {"clientSecret": "cs_test_secret_abc"})
        method, url, kw = calls[0]
        self.assertEqual((method, url), ("POST", "https://api.stripe.com/v1/checkout/sessions"))
        self.assertEqual(kw["auth"], ("sk_test_unit", ""))
        self.assertTrue(kw["headers"]["Stripe-Version"].endswith(".dahlia"))
        self.assertIn("Idempotency-Key", kw["headers"])
        form = dict(kw["data"])
        self.assertEqual(form["ui_mode"], "embedded_page")
        self.assertEqual(form["mode"], "payment")
        self.assertEqual(form["line_items[0][price_data][unit_amount]"], "2800", "client-sent price ignored")
        self.assertEqual(form["line_items[0][quantity]"], "2")
        self.assertEqual(form["line_items[1][price_data][unit_amount]"], "2800")
        self.assertEqual(form["shipping_address_collection[allowed_countries][0]"], "US")
        self.assertEqual(form["shipping_options[0][shipping_rate_data][fixed_amount][amount]"], str(595 + 200 * 2))
        self.assertEqual(form["return_url"], "https://weelect.org/shop/success?session_id={CHECKOUT_SESSION_ID}")
        self.assertEqual(form["metadata[cart]"], "tee.navy.M:2,hat.navy.One size:1")
        self.assertNotIn("automatic_tax[enabled]", form)

    def test_stripe_tax_flag(self):
        with mock.patch.dict(os.environ, {"STRIPE_TAX": "1"}), mock.patch("shop.requests.request", return_value=Resp(200, {"client_secret": "cs_x"})) as req:
            self.assertEqual(self.post([{"p": "tee", "c": "cream", "s": "L", "q": 1}]).status_code, 200)
        form = dict(req.call_args[1]["data"])
        self.assertEqual(form["automatic_tax[enabled]"], "true")
        self.assertEqual(form["line_items[0][price_data][tax_behavior]"], "exclusive")

    def test_fixed_tax_rate(self):
        with mock.patch.dict(os.environ, {"STRIPE_TAX_RATE_ID": "txr_123"}), mock.patch("shop.requests.request", return_value=Resp(200, {"client_secret": "cs_x"})) as req:
            self.post([{"p": "tee", "c": "cream", "s": "L", "q": 1}])
        self.assertEqual(dict(req.call_args[1]["data"])["line_items[0][tax_rates][0]"], "txr_123")

    def test_rejects_bad_carts(self):
        bad = [[], "x", [{"p": "mug", "c": "navy", "s": "M", "q": 1}], [{"p": "tee", "c": "pink", "s": "M", "q": 1}],
               [{"p": "tee", "c": "navy", "s": "XXXL", "q": 1}], [{"p": "tee", "c": "navy", "s": "M", "q": 0}],
               [{"p": "tee", "c": "navy", "s": "M", "q": 11}], [{"p": "tee", "c": "navy", "s": "M", "q": "2"}],
               [{"p": "tee", "c": "navy", "s": "M", "q": True}], [{"p": "tee", "c": "navy", "s": "M", "q": 6}, {"p": "tee", "c": "navy", "s": "M", "q": 6}]]
        with mock.patch("shop.requests.request") as req:
            for items in bad:
                r = self.post(items)
                self.assertEqual(r.status_code, 400, items)
            req.assert_not_called()

    def test_stripe_failure_gives_generic_error(self):
        with mock.patch("shop.requests.request", return_value=Resp(401, {"error": {"type": "invalid_request_error", "message": "Invalid API Key provided: sk_test_unit"}})):
            r = self.post([{"p": "tee", "c": "navy", "s": "M", "q": 1}])
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(b"sk_test", r.data)
        self.assertNotIn(b"Invalid API Key", r.data)

    def test_rate_limit(self):
        import app as appmod
        appmod._HITS.clear()
        with mock.patch("shop.requests.request", return_value=Resp(200, {"client_secret": "cs_x"})):
            codes = [self.post([{"p": "tee", "c": "navy", "s": "M", "q": 1}]).status_code for _ in range(14)]
        self.assertIn(429, codes)
        appmod._HITS.clear()


class SuccessTests(ShopBase):
    def test_success_page_never_shows_raw_errors(self):
        import shop
        with mock.patch("shop.requests.request", side_effect=shop.requests.ConnectionError("boom sk_test_unit")):
            r = self.c.get("/shop/success?session_id=" + SESSION_ID, base_url=self.base)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"boom", r.data)
        self.assertNotIn(b"sk_test", r.data)

    def test_bad_session_id_not_sent_to_stripe(self):
        with mock.patch("shop.requests.request") as req:
            r = self.c.get("/shop/success?session_id=../../v1/charges", base_url=self.base)
            self.assertEqual(r.status_code, 200)
            req.assert_not_called()

    def test_paid(self):
        with mock.patch("shop.requests.request", return_value=Resp(200, {"payment_status": "paid", "customer_details": {"email": "a@b.co"}})):
            r = self.c.get("/shop/success?session_id=" + SESSION_ID, base_url=self.base)
        self.assertIn(b"a@b.co", r.data)

    def test_cancel_page(self):
        self.assertEqual(self.c.get("/shop/cancel").status_code, 200)


class UnconfiguredTests(unittest.TestCase):
    """No env vars at all: the page works, the store says coming soon, nothing leaks, nothing crashes."""
    def setUp(self):
        keys = ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET", "PRINTFUL_API_TOKEN", "PRINTFUL_STORE_ID", "STRIPE_TAX"]
        self.patch = mock.patch.dict(os.environ, {k: "" for k in keys})
        self.patch.start()
        import app as appmod
        appmod._HITS.clear()
        self.c = appmod.app.test_client()

    def tearDown(self):
        self.patch.stop()

    def test_support_page_coming_soon(self):
        r = self.c.get("/support")
        self.assertEqual(r.status_code, 200)
        t = r.get_data(as_text=True)
        self.assertIn("Coming soon", t)
        self.assertIn("free for everyone and supported by readers", t)
        self.assertIn("https://ko-fi.com/weelect", t)
        self.assertNotIn("cart-checkout", t)
        self.assertNotIn("we_csrf", r.headers.get("Set-Cookie", ""))
        self.assertIn("not a charity", t.lower())

    def test_checkout_endpoints_closed(self):
        self.assertEqual(self.c.get("/shop/checkout").status_code, 302)
        r = self.c.post("/shop/checkout", json={"items": []})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.c.post("/stripe/webhook", data=b"{}").status_code, 404)

    def test_stripe_ready_but_no_webhook_secret_keeps_store_closed(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PUBLISHABLE_KEY": "pk_test_x"}):
            self.assertIn("Coming soon", self.c.get("/support").get_data(as_text=True))


class PageTests(ShopBase):
    def test_open_store_page_and_headers(self):
        r = self.c.get("/support", base_url=self.base)
        t = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Coming soon", t)
        self.assertIn('id="shop-data"', t)
        self.assertIn("We Elect tee", t)
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("frame-src", csp)
        self.assertIn("https://ko-fi.com", csp)
        self.assertIn("https://js.stripe.com", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("payment=(self", r.headers["Permissions-Policy"])
        self.assertIn("we_csrf", r.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", r.headers.get("Set-Cookie", ""))
        self.assertIn("SameSite=Strict", r.headers.get("Set-Cookie", ""))
        self.assertNotIn("sk_test", t)

    def test_other_pages_keep_default_headers(self):
        r = self.c.get("/credits")
        self.assertIn("payment=()", r.headers["Permissions-Policy"])
        self.assertNotIn("stripe", r.headers["Content-Security-Policy"])

    def test_csp_report_only_switch(self):
        with mock.patch.dict(os.environ, {"SHOP_CSP_REPORT_ONLY": "1"}):
            r = self.c.get("/support")
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)

    def test_nav_footer_sitemap_credits(self):
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn('<a href="/support">Support</a>', home)
        self.assertIn('href="/support">Support We Elect</a>', home)
        self.assertIn("Is your voice not being heard", home)
        self.assertIn("/support", self.c.get("/sitemap.xml").get_data(as_text=True))
        cr = self.c.get("/credits").get_data(as_text=True)
        self.assertIn('id="donations"', cr)
        self.assertIn("Printful", cr)
        self.assertEqual(self.c.get("/shop").status_code, 302)

    def test_catalog_integrity(self):
        import shop
        cat = shop.catalog()
        for p in cat["products"]:
            self.assertEqual(p["price_cents"], 2800)
            for c in p["colors"]:
                self.assertTrue(os.path.exists(os.path.join(os.path.dirname(shop.__file__), p["printful"]["files"][c["id"]].lstrip("/"))))
                self.assertTrue(os.path.exists(os.path.join(os.path.dirname(shop.__file__), c["image"].lstrip("/"))))
                for s in p["sizes"]:
                    self.assertIsInstance(p["variants"][f"{c['id']}:{s}"]["printful_variant_id"], int)

    def test_printful_shipping_rates_fallback(self):
        import shop
        with mock.patch("shop.requests.post", return_value=Resp(200, {"result": [{"id": "STANDARD", "rate": "4.75"}]})):
            rates, err = shop.printful_shipping_rates({"country_code": "US"}, [{"variant_id": 4112, "quantity": 1}])
        self.assertEqual(rates[0]["rate"], "4.75")
        with mock.patch("shop.requests.post", side_effect=shop.requests.ConnectionError("x")):
            rates, err = shop.printful_shipping_rates({"country_code": "US"}, [])
        self.assertIsNone(rates)
        self.assertEqual(err, "ConnectionError")


if __name__ == "__main__":
    unittest.main()

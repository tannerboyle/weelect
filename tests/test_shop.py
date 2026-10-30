"""Shop tests. No network and no real keys: PayPal and Printful HTTP calls go to an in-memory fake.
Run:  python -m unittest discover -s tests -v
"""
import copy, json, os, re, tempfile, time, unittest
from unittest import mock

TEST_ENV = {"PAYPAL_CLIENT_ID": "AcidUnitTest", "PAYPAL_CLIENT_SECRET": "EsecretUnitTest", "PAYPAL_WEBHOOK_ID": "WH-UNIT-1",
            "PAYPAL_ENV": "", "PRINTFUL_API_TOKEN": "pf_unit", "SITE_URL": "https://weelect.org", "SHOP_TAX_RATES": "",
            "PRINTFUL_AUTO_CONFIRM": "", "PRINTFUL_STORE_ID": ""}
ORDER_ID = "5O190127TN364715T"
SB = "https://api-m.sandbox.paypal.com"
CART = [{"p": "tee", "c": "navy", "s": "M", "q": 2}, {"p": "hat", "c": "navy", "s": "One size", "q": 1}]
ADDR = {"name": "Pat Reader", "line1": "1 Main St", "line2": "", "city": "Austin", "state": "TX", "zip": "78701"}
WH_HEADERS = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
              "PAYPAL-TRANSMISSION-ID": "t-1", "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "2026-10-29T20:00:00Z"}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body if body is not None else {}

    def json(self):
        return self._b


class FakeHTTP:
    """Just enough of PayPal (sandbox) and Printful to exercise shop.py."""
    def __init__(self):
        self.calls = []
        self.orders = {}               # PayPal order id -> order
        self.capture_status = "COMPLETED"
        self.capture_http = None       # e.g. (422, "INSTRUMENT_DECLINED")
        self.verify = "SUCCESS"
        self.verify_down = False
        self.create_http = 201
        self.pf_existing = set()
        self.pf_post = (200, {"code": 200, "result": {"id": 99, "status": "draft"}})
        self.pf_posts = []

    def order_from_create(self, body, oid=ORDER_ID):
        o = {"id": oid, "status": "CREATED", "intent": "CAPTURE", "purchase_units": copy.deepcopy(body["purchase_units"])}
        self.orders[oid] = o
        return o

    def _capture(self, oid):
        o = self.orders[oid]
        if o["status"] == "COMPLETED":
            return Resp(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
        if self.capture_http:
            return Resp(self.capture_http[0], {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": self.capture_http[1]}]})
        pu = o["purchase_units"][0]
        o["status"] = "COMPLETED" if self.capture_status == "COMPLETED" else o["status"]
        o["payer"] = {"email_address": "reader@example.com", "name": {"given_name": "Pat"}}
        pu["payments"] = {"captures": [{"id": "3C679366HH908993F", "status": self.capture_status, "amount": dict(pu["amount"]),
                                        "status_details": {"reason": "PENDING_REVIEW"} if self.capture_status == "PENDING" else None}]}
        pu["payments"]["captures"][0]["amount"].pop("breakdown", None)
        return Resp(201, o)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/v1/oauth2/token"):
            return Resp(200, {"access_token": "A21AAunit", "expires_in": 32400})
        if url.startswith("https://api.printful.com/orders"):
            self.pf_posts.append((url, json.loads(kw["data"])))
            st, body = self.pf_post
            if st == 200:
                self.pf_existing.add(json.loads(kw["data"])["external_id"])
            return Resp(st, body)
        if url.startswith("https://api.printful.com/shipping/rates"):
            return Resp(200, {"result": [{"id": "STANDARD", "rate": "4.75"}]})
        raise AssertionError("unexpected POST " + url)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if url.startswith("https://api.printful.com/orders/@"):
            return Resp(200, {"result": {"id": 1}}) if url.rsplit("@", 1)[1] in self.pf_existing else Resp(404, {"error": {"message": "Not found"}})
        raise AssertionError("unexpected GET " + url)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert url.startswith(SB) or url.startswith("https://api-m.paypal.com"), url
        path = url.split(".com", 1)[1]
        body = json.loads(kw["data"]) if kw.get("data") else None
        if method == "POST" and path == "/v2/checkout/orders":
            if self.create_http != 201:
                return Resp(self.create_http, {"name": "INVALID_REQUEST", "message": "Client Authentication failed EsecretUnitTest"})
            return Resp(201, {"id": self.order_from_create(body)["id"], "status": "CREATED"})
        m = re.match(r"^/v2/checkout/orders/([A-Z0-9]+)(/capture)?$", path)
        if m and m.group(2):
            return self._capture(m.group(1))
        if m and method == "GET":
            o = self.orders.get(m.group(1))
            return Resp(200, o) if o else Resp(404, {"name": "RESOURCE_NOT_FOUND"})
        if path == "/v1/notifications/verify-webhook-signature":
            if self.verify_down:
                raise __import__("requests").ConnectionError("down")
            return Resp(200, {"verification_status": self.verify})
        raise AssertionError("unexpected " + method + " " + url)

    def paypal_calls(self, frag):
        return [c for c in self.calls if frag in c[1]]


class ShopBase(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = dict(TEST_ENV, SHOP_ORDER_LOG=os.path.join(self.tmp, "orders.jsonl"), **self.extra_env)
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        import app as appmod, shop
        shop._TOKEN.update(key=None, token=None, exp=0)
        self.app = appmod.app
        self.c = self.app.test_client()
        self.base = "https://weelect.org"
        appmod._HITS.clear()
        self.fake = FakeHTTP()
        self.patches = [mock.patch("shop.requests.post", side_effect=self.fake.post), mock.patch("shop.requests.get", side_effect=self.fake.get),
                        mock.patch("shop.requests.request", side_effect=self.fake.request)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
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

    def api(self, path, body=None, token="auto", headers=None):
        tok = self.csrf() if token == "auto" else token
        h = dict({"X-CSRF-Token": tok} if tok else {}, **(headers or {}))
        return self.c.post(path, json=body or {}, headers=h, base_url=self.base)

    def create(self, items=CART, address=ADDR):
        return self.api("/shop/paypal/order", {"items": items, "address": address})

    def capture(self, oid=ORDER_ID):
        return self.api(f"/shop/paypal/order/{oid}/capture")

    def paid_order(self, items=CART, address=ADDR):
        """Create an order through our endpoint, then capture it at the fake PayPal directly (as the buyer's approval would)."""
        self.assertEqual(self.create(items, address).status_code, 200)
        self.fake._capture(ORDER_ID)
        return self.fake.orders[ORDER_ID]

    def webhook(self, event, headers=WH_HEADERS):
        return self.c.post("/paypal/webhook", data=json.dumps(event), headers=dict(headers, **{"Content-Type": "application/json"}), base_url=self.base)


def capture_event(oid=ORDER_ID, etype="PAYMENT.CAPTURE.COMPLETED"):
    return {"id": "WH-EVT-1", "event_type": etype, "resource_type": "capture",
            "resource": {"id": "3C679366HH908993F", "status": "COMPLETED", "supplementary_data": {"related_ids": {"order_id": oid}}}}


# ------------------------------------------------------------------ tax and address
class TaxTests(ShopBase):
    def test_no_rates_means_no_tax(self):
        import shop
        self.assertEqual(shop.tax_rates(), {})
        self.assertEqual(shop.tax_cents(8400, 995, "TX"), 0)
        self.assertEqual(shop.tax_mode(), "none")

    def test_rates_only_for_listed_states_with_rounding_and_shipping_option(self):
        import shop
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"wa": 10.25, "TX": {"rate": 8.25, "shipping": true}}'}):
            self.assertEqual(shop.tax_cents(8400, 995, "WA"), 861)            # 8400 * 10.25% = 861.0, shipping not taxed
            self.assertEqual(shop.tax_cents(8400, 995, "TX"), 775)            # (8400+995) * 8.25% = 775.09
            self.assertEqual(shop.tax_cents(2800, 595, "CA"), 0)              # not listed: no tax
            self.assertEqual(shop.tax_cents(2, 0, "WA"), 0)                   # 0.205 cents rounds to 0
            self.assertEqual(shop.tax_mode(), "state_rates")

    def test_bad_config_collects_nothing(self):
        import shop
        for raw in ['{"WA": 10.25', '[1,2]', '{"ZZ": 5}', '{"WA": "abc"}', '{"WA": 45}', '{"WA": -1}', '{"WA": true}']:
            with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": raw}):
                self.assertEqual(shop.tax_rates(), {}, raw)


class AddressTests(unittest.TestCase):
    def test_validation(self):
        import shop
        ok, err = shop.parse_address(dict(ADDR, state="tx", zip="78701-1234", line2="Apt 2\n"))
        self.assertIsNone(err)
        self.assertEqual((ok["state"], ok["line2"]), ("TX", "Apt 2"))
        for bad in [None, "x", dict(ADDR, name=""), dict(ADDR, line1=" "), dict(ADDR, city=""), dict(ADDR, state="ON"),
                    dict(ADDR, state="PR"), dict(ADDR, zip="7870"), dict(ADDR, zip="ABCDE"), dict(ADDR, line1="x" * 200), dict(ADDR, name=5)]:
            self.assertIsNone(shop.parse_address(bad)[0], bad)


# ------------------------------------------------------------------ create order
class CreateOrderTests(ShopBase):
    def test_server_side_prices_and_itemized_breakdown(self):
        tampered = [dict(CART[0], price=1, price_cents=1, unit_amount=1), CART[1]]
        r = self.create(tampered)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["id"], ORDER_ID)
        method, url, kw = self.fake.paypal_calls("/v2/checkout/orders")[0]
        self.assertEqual((method, url), ("POST", SB + "/v2/checkout/orders"))
        self.assertEqual(kw["headers"]["Authorization"], "Bearer A21AAunit")
        self.assertTrue(kw["headers"]["PayPal-Request-Id"].startswith("we-create-"))
        body = json.loads(kw["data"])
        self.assertEqual(body["intent"], "CAPTURE")
        self.assertEqual(body["application_context"]["shipping_preference"], "SET_PROVIDED_ADDRESS")
        pu = body["purchase_units"][0]
        self.assertEqual(pu["custom_id"], "weelect-shop")
        self.assertEqual([(i["sku"], i["quantity"], i["unit_amount"]["value"]) for i in pu["items"]],
                         [("tee.navy.M", "2", "28.00"), ("hat.navy.One size", "1", "28.00")], "client-sent prices ignored")
        bd = pu["amount"]["breakdown"]
        self.assertEqual((bd["item_total"]["value"], bd["shipping"]["value"], bd["tax_total"]["value"]), ("84.00", "9.95", "0.00"))
        self.assertEqual(pu["amount"]["value"], "93.95")
        self.assertEqual(pu["shipping"]["address"], {"address_line_1": "1 Main St", "admin_area_2": "Austin", "admin_area_1": "TX",
                                                     "postal_code": "78701", "country_code": "US"})
        self.assertEqual(pu["shipping"]["name"]["full_name"], "Pat Reader")
        tok = [c for c in self.fake.calls if c[1].endswith("/v1/oauth2/token")][0]
        self.assertEqual(tok[2]["auth"], ("AcidUnitTest", "EsecretUnitTest"))

    def test_breakdown_adds_up_with_tax(self):
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"TX": {"rate": 8.25, "shipping": true}}'}):
            self.assertEqual(self.create().status_code, 200)
        pu = json.loads(self.fake.paypal_calls("/v2/checkout/orders")[0][2]["data"])["purchase_units"][0]
        bd = {k: v["value"] for k, v in pu["amount"]["breakdown"].items()}
        self.assertEqual(bd, {"item_total": "84.00", "shipping": "9.95", "tax_total": "7.75"})
        self.assertEqual(pu["amount"]["value"], "101.70")
        items = sum(float(i["unit_amount"]["value"]) * int(i["quantity"]) for i in pu["items"])
        self.assertAlmostEqual(items, 84.00)

    def test_live_env_uses_live_api(self):
        with mock.patch.dict(os.environ, {"PAYPAL_ENV": "live"}):
            self.create()
        self.assertTrue(any(c[1] == "https://api-m.paypal.com/v2/checkout/orders" for c in self.fake.calls))

    def test_rejects_bad_carts_and_addresses(self):
        bad = [[], "x", [{"p": "mug", "c": "navy", "s": "M", "q": 1}], [{"p": "tee", "c": "pink", "s": "M", "q": 1}],
               [{"p": "tee", "c": "navy", "s": "XXXL", "q": 1}], [{"p": "tee", "c": "navy", "s": "M", "q": 0}],
               [{"p": "tee", "c": "navy", "s": "M", "q": 11}], [{"p": "tee", "c": "navy", "s": "M", "q": "2"}],
               [{"p": "tee", "c": "navy", "s": "M", "q": True}], [{"p": "tee", "c": "navy", "s": "M", "q": 6}, {"p": "tee", "c": "navy", "s": "M", "q": 6}]]
        for items in bad:
            self.assertEqual(self.create(items).status_code, 400, items)
        self.assertEqual(self.create(CART, dict(ADDR, state="ZZ")).status_code, 400)
        self.assertEqual(self.fake.paypal_calls("/v2/checkout/orders"), [])

    def test_requires_csrf_and_same_origin(self):
        self.csrf()
        self.assertEqual(self.api("/shop/paypal/order", {"items": CART, "address": ADDR}, token=None).status_code, 403)
        self.assertEqual(self.api("/shop/paypal/order", {"items": CART, "address": ADDR}, token="x.y.z").status_code, 403)
        r = self.api("/shop/paypal/order", {"items": CART, "address": ADDR}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_expired_token_rejected(self):
        import shop
        tok = self.csrf()
        with mock.patch("shop.time.time", return_value=time.time() + shop.CSRF_TTL + 60):
            r = self.api("/shop/paypal/order", {"items": CART, "address": ADDR}, token=tok)
        self.assertEqual(r.status_code, 403)

    def test_paypal_failure_gives_generic_error(self):
        self.fake.create_http = 401
        r = self.create()
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(b"Esecret", r.data)
        self.assertNotIn(b"Client Authentication", r.data)

    def test_rate_limit(self):
        import app as appmod
        appmod._HITS.clear()
        codes = [self.create().status_code for _ in range(14)]
        self.assertIn(429, codes)
        appmod._HITS.clear()

    def test_quote_matches_order(self):
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"TX": 8.25}'}):
            q = self.api("/shop/quote", {"items": CART, "address": ADDR}).get_json()
            self.assertEqual((q["items"], q["shipping"], q["tax"], q["total"]), ("$84.00", "$9.95", "$6.93", "$100.88"))
            q2 = self.api("/shop/quote", {"items": CART, "address": dict(ADDR, state="OR", zip="97201")}).get_json()
            self.assertEqual((q2["tax"], q2["total"]), ("$0.00", "$93.95"))
        self.assertEqual(self.fake.calls, [], "quotes never call PayPal")


# ------------------------------------------------------------------ capture (browser path)
class CaptureTests(ShopBase):
    def test_capture_completed_creates_one_draft_with_paid_address(self):
        self.assertEqual(self.create().status_code, 200)
        r = self.capture()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json(), {"status": "completed", "redirect": "/shop/success?order=" + ORDER_ID})
        cap = self.fake.paypal_calls("/capture")[0]
        self.assertEqual(cap[2]["headers"]["PayPal-Request-Id"], "we-capture-" + ORDER_ID)
        self.assertEqual(len(self.fake.pf_posts), 1)
        url, payload = self.fake.pf_posts[0]
        self.assertEqual(url, "https://api.printful.com/orders", "draft by default")
        self.assertEqual(payload["external_id"], "pp" + ORDER_ID)
        self.assertLessEqual(len(payload["external_id"]), 32)
        self.assertEqual(payload["recipient"], {"name": "Pat Reader", "address1": "1 Main St", "address2": "", "city": "Austin", "state_code": "TX",
                                                "country_code": "US", "zip": "78701", "email": "reader@example.com"})
        items = {i["variant_id"]: i for i in payload["items"]}
        self.assertEqual(items[4112]["quantity"], 2, "navy M tee variant")
        self.assertEqual(items[7857]["quantity"], 1, "navy dad hat variant")
        self.assertEqual(items[4112]["retail_price"], "28.00")
        self.assertTrue(items[4112]["files"][0]["url"].startswith("https://weelect.org/static/shop/print/"))
        self.assertEqual(payload["retail_costs"], {"currency": "USD", "subtotal": "84.00", "shipping": "9.95", "tax": "0.00"})
        rec = self.log_records()[-1]
        self.assertEqual((rec["state"], rec["printful_id"], rec["confirmed"], rec["amounts_match"]), ("created", 99, False, True))
        self.assertNotIn("Main St", json.dumps(self.log_records()), "no addresses in the log")

    def test_repeat_capture_is_idempotent(self):
        self.create()
        self.assertEqual(self.capture().status_code, 200)
        self.assertEqual(self.capture().status_code, 200)       # double click / retry: PayPal says ORDER_ALREADY_CAPTURED
        self.assertEqual(len(self.fake.pf_posts), 1)

    def test_idempotent_across_restarts_via_printful_external_id(self):
        self.create()
        self.capture()
        os.remove(os.environ["SHOP_ORDER_LOG"])                 # new worker / redeploy: local log gone
        self.assertEqual(self.webhook(capture_event()).status_code, 200)
        self.assertEqual(len(self.fake.pf_posts), 1, "Printful lookup by @external_id stops the duplicate")

    def test_capture_pending_does_not_fulfil(self):
        self.fake.capture_status = "PENDING"
        self.create()
        r = self.capture()
        self.assertEqual(r.get_json()["status"], "pending")
        self.assertEqual(self.fake.pf_posts, [])
        self.assertEqual(self.log_records()[-1]["state"], "pending")

    def test_capture_declined_asks_buyer_to_retry(self):
        self.fake.capture_http = (422, "INSTRUMENT_DECLINED")
        self.create()
        r = self.capture()
        self.assertEqual(r.status_code, 402)
        self.assertTrue(r.get_json()["restart"])
        self.assertEqual(self.fake.pf_posts, [])

    def test_capture_status_declined_does_not_fulfil(self):
        self.fake.capture_status = "DECLINED"
        self.create()
        self.assertEqual(self.capture().status_code, 402)
        self.assertEqual(self.fake.pf_posts, [])

    def test_capture_other_failure(self):
        self.fake.capture_http = (422, "ORDER_NOT_APPROVED")
        self.create()
        r = self.capture()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.fake.pf_posts, [])

    def test_bad_or_foreign_order_ids(self):
        self.assertEqual(self.capture("../../v1/payments").status_code, 404)    # not even routed
        self.assertEqual(self.capture("abc").status_code, 400)
        self.fake.orders["9ZZ90127TN364715T"] = {"id": "9ZZ90127TN364715T", "status": "APPROVED",
                                                 "purchase_units": [dict(json.loads(json.dumps({"custom_id": "someone-else", "amount": {"currency_code": "USD", "value": "1.00"}, "items": []})))]}
        self.assertEqual(self.capture("9ZZ90127TN364715T").status_code, 400)
        self.assertEqual(self.fake.pf_posts, [])

    def test_price_tampered_order_is_never_auto_confirmed(self):
        """If the paid amount does not equal what the catalog says (e.g. an order created outside our endpoint), the order
        still reaches Printful as a draft for review but is never auto-confirmed, and the log flags it."""
        with mock.patch.dict(os.environ, {"PRINTFUL_AUTO_CONFIRM": "1"}):
            self.create()
            pu = self.fake.orders[ORDER_ID]["purchase_units"][0]
            pu["items"][0]["unit_amount"]["value"] = "0.01"
            pu["amount"]["value"] = "37.97"
            self.capture()
        url, _ = self.fake.pf_posts[0]
        self.assertEqual(url, "https://api.printful.com/orders")
        self.assertFalse(self.log_records()[-1]["amounts_match"])

    def test_quantity_tampered_order_is_not_fulfilled(self):
        self.create()
        self.fake.orders[ORDER_ID]["purchase_units"][0]["items"][0]["quantity"] = "999"
        self.capture()
        self.assertEqual(self.fake.pf_posts, [])
        self.assertEqual(self.log_records()[-1]["state"], "failed_permanent")

    def test_auto_confirm_flag(self):
        with mock.patch.dict(os.environ, {"PRINTFUL_AUTO_CONFIRM": "1"}):
            self.create()
            self.capture()
        self.assertEqual(self.fake.pf_posts[0][0], "https://api.printful.com/orders?confirm=true")

    def test_printful_down_buyer_still_succeeds_and_webhook_retries(self):
        self.fake.pf_post = (503, {})
        self.create()
        r = self.capture()
        self.assertEqual(r.get_json()["status"], "completed")
        self.assertEqual(self.log_records()[-1]["state"], "retrying")
        self.fake.pf_post = (200, {"result": {"id": 8, "status": "draft"}})
        self.assertEqual(self.webhook(capture_event()).status_code, 200)
        self.assertEqual(self.log_records()[-1]["state"], "created")

    def test_printful_rejection_logged_not_retried(self):
        self.fake.pf_post = (400, {"error": {"message": "Variant not available"}})
        self.create()
        self.capture()
        self.assertEqual(self.webhook(capture_event()).status_code, 200)
        self.assertEqual(len(self.fake.pf_posts), 1)
        self.assertEqual(self.log_records()[-1]["state"], "failed_permanent")

    def test_missing_printful_token_logs_for_manual_order(self):
        with mock.patch.dict(os.environ, {"PRINTFUL_API_TOKEN": ""}):
            self.create()
            self.assertEqual(self.capture().get_json()["status"], "completed")
        self.assertEqual(self.fake.pf_posts, [])
        self.assertEqual(self.log_records()[-1]["state"], "failed_permanent")


# ------------------------------------------------------------------ webhook (backstop)
class WebhookTests(ShopBase):
    def test_signature_failure_rejected(self):
        self.paid_order()
        self.fake.verify = "FAILURE"
        r = self.webhook(capture_event())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.pf_posts, [])
        body = json.loads(self.fake.paypal_calls("verify-webhook-signature")[0][2]["data"])
        self.assertEqual(body["webhook_id"], "WH-UNIT-1")
        self.assertEqual(body["transmission_id"], "t-1")
        self.assertEqual(body["webhook_event"]["event_type"], "PAYMENT.CAPTURE.COMPLETED")

    def test_missing_headers_rejected_without_calling_paypal(self):
        r = self.webhook(capture_event(), headers={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.paypal_calls("verify-webhook-signature"), [])

    def test_verification_service_down_asks_for_retry(self):
        self.fake.verify_down = True
        self.assertEqual(self.webhook(capture_event()).status_code, 503)

    def test_capture_completed_event_fulfils_once(self):
        self.paid_order()
        self.assertEqual(self.webhook(capture_event()).status_code, 200)
        self.assertEqual(self.webhook(capture_event()).status_code, 200)     # PayPal redelivery
        self.assertEqual(len(self.fake.pf_posts), 1)
        self.assertEqual(self.fake.pf_posts[0][1]["external_id"], "pp" + ORDER_ID)

    def test_webhook_and_browser_race_creates_one_order(self):
        self.create()
        self.capture()
        self.webhook(capture_event())
        self.webhook({"id": "WH-4", "event_type": "CHECKOUT.ORDER.COMPLETED", "resource": copy.deepcopy(self.fake.orders[ORDER_ID])})
        self.assertEqual(len(self.fake.pf_posts), 1)

    def test_order_approved_event_captures_and_fulfils(self):
        """Buyer approved but closed the tab before our capture call: the webhook captures and fulfils."""
        self.create()
        res = copy.deepcopy(self.fake.orders[ORDER_ID])
        res["status"] = "APPROVED"
        r = self.webhook({"id": "WH-2", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": res})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.fake.paypal_calls("/capture")[0][2]["headers"]["PayPal-Request-Id"], "we-capture-" + ORDER_ID)
        self.assertEqual(len(self.fake.pf_posts), 1)
        self.capture()                                    # the browser shows up late: no second order
        self.assertEqual(len(self.fake.pf_posts), 1)

    def test_approved_foreign_order_is_not_captured(self):
        r = self.webhook({"id": "WH-3", "event_type": "CHECKOUT.ORDER.APPROVED",
                          "resource": {"id": ORDER_ID, "purchase_units": [{"custom_id": "other-app"}]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.paypal_calls("/capture"), [])

    def test_pending_capture_via_webhook_not_fulfilled(self):
        self.fake.capture_status = "PENDING"
        self.paid_order()
        self.assertEqual(self.webhook(capture_event()).status_code, 200)
        self.assertEqual(self.fake.pf_posts, [])

    def test_refund_logged_other_events_ignored_bad_payload(self):
        ev = capture_event(etype="PAYMENT.CAPTURE.REFUNDED")
        self.assertEqual(self.webhook(ev).status_code, 200)
        self.assertEqual(self.log_records()[-1]["event"], "PAYMENT.CAPTURE.REFUNDED")
        self.assertEqual(self.webhook({"id": "x", "event_type": "BILLING.PLAN.CREATED", "resource": {}}).status_code, 200)
        r = self.c.post("/paypal/webhook", data=b"not json", headers=WH_HEADERS, base_url=self.base)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.pf_posts, [])


# ------------------------------------------------------------------ success page
class SuccessTests(ShopBase):
    def test_paid(self):
        self.paid_order()
        r = self.c.get("/shop/success?order=" + ORDER_ID, base_url=self.base)
        self.assertIn(b"reader@example.com", r.data)
        self.assertIn(b"Your order is in", r.data)

    def test_pending(self):
        self.fake.capture_status = "PENDING"
        self.paid_order()
        self.assertIn(b"still confirming", self.c.get("/shop/success?order=" + ORDER_ID, base_url=self.base).data)

    def test_errors_and_bad_ids_never_leak(self):
        r = self.c.get("/shop/success?order=" + ORDER_ID, base_url=self.base)    # unknown order -> 404 at PayPal
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"RESOURCE_NOT_FOUND", r.data)
        n = len(self.fake.calls)
        self.assertEqual(self.c.get("/shop/success?order=../../v1/x", base_url=self.base).status_code, 200)
        self.assertEqual(len(self.fake.calls), n, "bad id never sent to PayPal")


# ------------------------------------------------------------------ unconfigured and pages
class UnconfiguredTests(unittest.TestCase):
    """No env vars at all: the page works, the store says coming soon, nothing leaks, nothing crashes."""
    def setUp(self):
        keys = ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "PRINTFUL_API_TOKEN", "PRINTFUL_STORE_ID", "SHOP_TAX_RATES"]
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

    def test_endpoints_closed(self):
        with mock.patch("shop.requests.request") as req, mock.patch("shop.requests.post") as post:
            self.assertEqual(self.c.get("/shop/checkout").status_code, 302)
            self.assertEqual(self.c.post("/shop/paypal/order", json={}).status_code, 503)
            self.assertEqual(self.c.post("/shop/quote", json={}).status_code, 503)
            self.assertEqual(self.c.post(f"/shop/paypal/order/{ORDER_ID}/capture", json={}).status_code, 503)
            self.assertEqual(self.c.post("/paypal/webhook", data=b"{}").status_code, 404)
            self.assertEqual(self.c.get("/shop/success?order=" + ORDER_ID).status_code, 200)
            req.assert_not_called()
            post.assert_not_called()

    def test_paypal_creds_without_webhook_id_keep_store_closed(self):
        with mock.patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y"}):
            self.assertIn("Coming soon", self.c.get("/support").get_data(as_text=True))


class PageTests(ShopBase):
    def test_open_store_page_and_headers(self):
        r = self.c.get("/support", base_url=self.base)
        t = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Coming soon", t)
        self.assertIn('id="shop-data"', t)
        self.assertIn("We Elect tee", t)
        self.assertIn("PayPal", t)
        self.assertNotIn("Stripe", t)
        csp = r.headers["Content-Security-Policy"]
        for frag in ("script-src 'self' 'unsafe-inline' https://*.paypal.com https://*.paypalobjects.com https://gc.zgo.at",
                     "frame-src https://*.paypal.com https://*.paypalobjects.com https://ko-fi.com",
                     "connect-src 'self' https://*.paypal.com https://*.paypalobjects.com https://weelect.goatcounter.com",
                     "form-action 'self'", "frame-ancestors 'none'", "object-src 'none'"):
            self.assertIn(frag, csp)
        self.assertNotIn("stripe", csp)
        self.assertIn('payment=(self "https://www.paypal.com"', r.headers["Permissions-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        ck = r.headers.get("Set-Cookie", "")
        self.assertIn("we_csrf", ck)
        self.assertIn("HttpOnly", ck)
        self.assertIn("SameSite=Strict", ck)
        self.assertNotIn("EsecretUnitTest", t)

    def test_checkout_page(self):
        r = self.c.get("/shop/checkout", base_url=self.base)
        t = r.get_data(as_text=True)
        self.assertIn('data-client-id="AcidUnitTest"', t)
        self.assertNotIn("EsecretUnitTest", t)
        self.assertIn('<option value="TX">Texas</option>', t)
        self.assertIn("shop-checkout.js", t)
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_tax_states_listed_in_fine_print(self):
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": 10.25}'}):
            self.assertIn("orders shipped to WA", self.c.get("/support").get_data(as_text=True))
        self.assertNotIn("Sales tax is added", self.c.get("/support").get_data(as_text=True))

    def test_other_pages_keep_default_headers(self):
        r = self.c.get("/credits")
        self.assertIn("payment=()", r.headers["Permissions-Policy"])
        self.assertNotIn("paypal", r.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_csp_report_only_switch(self):
        with mock.patch.dict(os.environ, {"SHOP_CSP_REPORT_ONLY": "1"}):
            r = self.c.get("/support")
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)

    def test_nav_footer_sitemap_credits(self):
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn('<a href="/support">Support</a>', home)
        self.assertIn('href="/support">Support We Elect</a>', home)
        self.assertIn("weelect.goatcounter.com", home)
        self.assertIn("/support", self.c.get("/sitemap.xml").get_data(as_text=True))
        cr = self.c.get("/credits").get_data(as_text=True)
        self.assertIn('id="donations"', cr)
        self.assertIn("Printful", cr)
        self.assertIn("PayPal", cr)
        self.assertNotIn("Stripe", cr)
        self.assertEqual(self.c.get("/shop").status_code, 302)

    def test_no_stripe_left(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for d, _, files in os.walk(root):
            if any(x in d for x in (".git", "tests", "docs", "__pycache__")):
                continue
            for f in files:
                if f.endswith((".py", ".html", ".js", ".json")):
                    with open(os.path.join(d, f), encoding="utf-8", errors="ignore") as fh:
                        self.assertNotIn("stripe.com", fh.read().lower(), os.path.join(d, f))

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
                    self.assertLessEqual(len(f"{p['id']}.{c['id']}.{s}"), 127, "fits PayPal's sku limit")

    def test_printful_shipping_rates(self):
        import shop
        rates, err = shop.printful_shipping_rates({"country_code": "US"}, [{"variant_id": 4112, "quantity": 1}])
        self.assertEqual(rates[0]["rate"], "4.75")
        with mock.patch("shop.requests.post", side_effect=shop.requests.ConnectionError("x")):
            rates, err = shop.printful_shipping_rates({"country_code": "US"}, [])
        self.assertIsNone(rates)
        self.assertEqual(err, "ConnectionError")


if __name__ == "__main__":
    unittest.main()

"""Shop tests. No network and no real keys: PayPal and Printful HTTP calls go to an in-memory fake.
Run:  python -m unittest discover -s tests -v
"""
import copy, json, os, re, tempfile, time, unittest
from unittest import mock

TEST_ENV = {"PAYPAL_CLIENT_ID": "AcidUnitTest", "PAYPAL_CLIENT_SECRET": "EsecretUnitTest", "PAYPAL_WEBHOOK_ID": "WH-UNIT-1",
            "PAYPAL_ENV": "", "PRINTFUL_API_TOKEN": "pf_unit", "SITE_URL": "https://weelect.org", "SHOP_TAX_RATES": "",
            "PRINTFUL_AUTO_CONFIRM": "", "PRINTFUL_STORE_ID": "", "SHOP_LIVE_CATALOG": "0", "SHOP_OPEN": "1"}
ORDER_ID = "5O190127TN364715T"
SB = "https://api-m.sandbox.paypal.com"
CART = [{"p": "tee", "c": "heather-navy", "s": "M", "q": 2}, {"p": "cap", "c": "black", "s": "One size", "q": 1}]
TEE_NAVY_M = (8510, 5613146975)      # (Printful catalog variant id, sync variant id) in the committed snapshot
CAP = (53139, 5613156721)
ADDR = {"name": "Pat Reader", "line1": "1 Main St", "line2": "", "city": "Austin", "state": "TX", "zip": "78701"}
WA_ADDR = {"name": "Sam Olalla", "line1": "12345 Olalla Valley Rd SE", "line2": "", "city": "Olalla", "state": "WA", "zip": "98359"}
DOR_OK = ('<?xml version="1.0" encoding="utf-8"?><response loccode="1800" localrate=".027" rate=".092" code="0" xmlns="">'
          '<addressline code="1800" street="OLALLA VALLEY RD SE" househigh="12345" houselow="12345" evenodd="O" state="WA" zip="98359" '
          'period="Q42026" rta="N" ptba="Kitsap PTBA" cez="" /><rate name="KITSAP PTBA" code="1800" staterate=".065" localrate=".027" /></response>')
DOR_SEATTLE = ('<?xml version="1.0" encoding="utf-8"?><response loccode="1726" localrate=".0405" rate=".1055" code="2" xmlns="">'
               '<addressline code="1726" street="UNION ST" househigh="601" houselow="601" evenodd="O" state="WA" zip="98101" plus4="2341" '
               'period="Q42026" rta="Y" ptba="King PTBA" cez="" /><rate name="SEATTLE" code="1726" staterate=".0650" localrate=".0405" /></response>')
DOR_BAD_ZIP = '<?xml version="1.0" encoding="utf-8"?><response loccode="-1" localrate="-1" rate="-1" code="9" debughint="Zipcode is invalid for WA State." xmlns="" />'
DOR_INTERNAL = '<?xml version="1.0" encoding="utf-8"?><response loccode="-1" localrate="-1" rate="-1" code="9" xmlns="" />'
DOR_URL = "https://webgis.dor.wa.gov/webapi/AddressRates.aspx"
DOR_ZIP_ONLY = ('<?xml version="1.0" encoding="utf-8"?><response loccode="4100" localrate=".017" rate=".082" code="5" xmlns="">'
                '<addressline code="4100" street="FAKE RD" househigh="1" houselow="1" evenodd="O" state="WA" zip="98359" '
                'period="Q42026" rta="N" ptba="" cez="" /><rate name="PIERCE COUNTY" code="4100" staterate=".065" localrate=".017" /></response>')
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
        self.dor = (200, DOR_OK)       # WA DOR address rate lookup: (status, xml) or an exception instance

    def dor_calls(self):
        return [c for c in self.calls if c[1].startswith(DOR_URL)]

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
        if url.startswith(DOR_URL):
            if isinstance(self.dor, Exception):
                raise self.dor
            r = Resp(self.dor[0])
            r.text = self.dor[1]
            return r
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
        import app as appmod, shop, printful_store, wa_tax
        wa_tax.reset()
        shop._TOKEN.update(key=None, token=None, exp=0)
        shop._SHIP.clear()
        printful_store.reset()
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
    """Washington destination-based sales tax: DOR address lookup, SHOP_TAX_RATES fallback, refusal with no rate."""
    def test_dor_parse_and_lookup_success(self):
        import wa_tax
        r = wa_tax.parse(DOR_OK)
        self.assertEqual((r["rate"], r["loccode"], r["code"], r["period"], r["name"]), (__import__("decimal").Decimal(".092"), "1800", 0, "Q42026", "KITSAP PTBA"))
        self.assertEqual(wa_tax.parse(DOR_SEATTLE)["rate"], __import__("decimal").Decimal(".1055"))
        self.assertRaises(wa_tax.AddressNotFound, wa_tax.parse, DOR_BAD_ZIP)
        self.assertRaises(wa_tax.LookupFailed, wa_tax.parse, DOR_INTERNAL)
        for bad in ["", "<html>busy</html>", "not xml", '<response code="0" rate=".5" loccode="1800"/>',
                    '<response code="0" rate=".092" loccode="x"/>', '<!DOCTYPE x [<!ENTITY a "b">]><response code="0"/>']:
            self.assertRaises(wa_tax.LookupFailed, wa_tax.parse, bad)

    def test_wa_lookup_success_taxes_items_and_shipping(self):
        import shop
        q = self.api("/shop/quote", {"items": CART, "address": WA_ADDR}).get_json()
        # (89.85 + 4.75) * 9.2% = 8.7032 -> $8.70
        self.assertEqual((q["items"], q["shipping"], q["tax"], q["total"]), ("$89.85", "$4.75", "$8.70", "$103.30"))
        self.assertEqual(q["tax_label"], "WA sales tax (9.2%)")
        call = self.fake.dor_calls()[0]
        self.assertEqual(call[2]["params"], {"output": "xml", "addr": "12345 Olalla Valley Rd SE", "city": "Olalla", "zip": "98359"})
        self.assertLessEqual(call[2]["timeout"], 10)
        t = shop.tax_info(WA_ADDR)
        self.assertEqual((shop.fmt_pct(t["rate_pct"]), t["loccode"], t["source"], t["period"]), ("9.2", "1800", "dor", "Q42026"))

    def test_lookups_are_cached(self):
        for _ in range(3):
            self.assertEqual(self.api("/shop/quote", {"items": CART, "address": WA_ADDR}).status_code, 200)
        self.assertEqual(self.create(CART, WA_ADDR).status_code, 200)
        self.assertEqual(len(self.fake.dor_calls()), 1)

    def test_lookup_failure_uses_zip_fallback_then_default(self):
        import shop, requests
        self.fake.dor = requests.Timeout("slow")
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": {"rate": 8.0, "zips": {"98359": 9.2}}}'}):
            t = shop.tax_info(WA_ADDR)
            self.assertEqual((t["rate_pct"], t["source"], t["loccode"]), (__import__("decimal").Decimal("9.2"), "fallback_zip", None))
            t = shop.tax_info(dict(WA_ADDR, zip="98501", city="Olympia"))
            self.assertEqual((shop.fmt_pct(t["rate_pct"]), t["source"]), ("8", "fallback_default"))
        for dor in [(500, "oops"), (200, DOR_INTERNAL), (200, "<html>maintenance</html>"), requests.ConnectionError("down")]:
            import wa_tax
            wa_tax.reset()
            self.fake.dor = dor
            with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": 9.0}'}):
                q = self.api("/shop/quote", {"items": CART, "address": WA_ADDR})
            self.assertEqual(q.status_code, 200, dor)
            self.assertEqual(q.get_json()["tax"], "$8.51", dor)          # 94.60 * 9% = 8.514

    def test_lookup_failure_without_fallback_blocks_wa_checkout(self):
        import requests
        self.fake.dor = requests.Timeout("slow")
        for raw in ["", '{"WA": 2}', '{"WA": 45}', '{"WA": "abc"}', '{"WA": true}', '{"TX": 8.25}', '{"WA": 10.25', '[1]',
                    '{"WA": {"zips": {"98101": 10.55}}}']:
            with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": raw}):
                r = self.api("/shop/quote", {"items": CART, "address": WA_ADDR})
                self.assertEqual(r.status_code, 503, raw)
                self.assertIn("Washington sales tax", r.get_json()["error"])
                self.assertEqual(self.create(CART, WA_ADDR).status_code, 503, raw)
        self.assertEqual(self.fake.paypal_calls("/v2/checkout/orders"), [], "no PayPal order without a WA rate")
        self.assertEqual([c for c in self.fake.calls if "shipping/rates" in c[1]], [], "refused before asking Printful")
        self.assertEqual(len(self.fake.dor_calls()), 1, "a failed lookup is remembered briefly, not retried on every click")

    def test_address_not_in_wa_asks_buyer_to_fix_it(self):
        self.fake.dor = (200, DOR_BAD_ZIP)
        r = self.api("/shop/quote", {"items": CART, "address": dict(WA_ADDR, zip="97201")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("could not find that address in Washington", r.get_json()["error"])

    def test_non_wa_addresses_pay_no_tax_and_never_call_dor(self):
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": 9.0, "TX": 8.25, "OR": 5}'}):
            for st, z in [("TX", "78701"), ("OR", "97201"), ("CA", "94103"), ("ID", "83814")]:
                q = self.api("/shop/quote", {"items": CART, "address": dict(ADDR, state=st, zip=z)}).get_json()
                self.assertEqual((q["tax"], q["total"], q["tax_label"]), ("$0.00", "$94.60", "Sales tax"), st)
        self.assertEqual(self.fake.dor_calls(), [])

    def test_zip_only_match_charges_higher_of_dor_and_fallback(self):
        import shop, wa_tax
        from decimal import Decimal
        for code in ("5", "1", "3"):
            for raw, want_pct, want_src, why in [
                    ('{"WA": {"rate": 8.0, "zips": {"98359": 9.2}}}', "9.2", "fallback_higher", "SHOP_TAX_RATES zip 98359 9.2%; charged the higher"),
                    ('{"WA": 9.5}', "9.5", "fallback_higher", "SHOP_TAX_RATES WA default 9.5%; charged the higher"),
                    ('{"WA": {"rate": 9.9, "zips": {"98359": 7.0}}}', "8.2", "dor", ">= SHOP_TAX_RATES zip 98359 7%; charged dor"),
                    ('{"WA": 8.0}', "8.2", "dor", ">= SHOP_TAX_RATES WA default 8%; charged dor"),
                    ("", "8.2", "dor", "no fallback set; charged dor")]:
                wa_tax.reset()
                self.fake.dor = (200, DOR_ZIP_ONLY.replace('code="5"', f'code="{code}"', 1))
                with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": raw}):
                    t = shop.tax_info(WA_ADDR)
                self.assertEqual((shop.fmt_pct(t["rate_pct"]), t["source"], t["result_code"]), (want_pct, want_src, int(code)), (code, raw))
                self.assertIn(why, t["reason"])
                self.assertIn(f"zip-only match (code {code})", t["reason"])
        # a full street match (code 0) never uses the fallback, even if it is higher
        wa_tax.reset()
        self.fake.dor = (200, DOR_OK)
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": 10.0}'}):
            t = shop.tax_info(WA_ADDR)
        self.assertEqual((shop.fmt_pct(t["rate_pct"]), t["source"], t["reason"]), ("9.2", "dor", "dor address match"))
        # end to end: the higher rate is charged, put on the PayPal order and logged with the reason
        wa_tax.reset()
        self.fake.dor = (200, DOR_ZIP_ONLY)
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": {"zips": {"98359": 9.2}}}'}):
            self.assertEqual(self.create(CART, dict(WA_ADDR, line1="1 Fake Rd")).status_code, 200)
        pu = json.loads(self.fake.paypal_calls("/v2/checkout/orders")[0][2]["data"])["purchase_units"][0]
        self.assertEqual(pu["amount"]["breakdown"]["tax_total"]["value"], "8.70")      # 94.60 * 9.2%, not 8.2% ($7.76)
        rec = self.log_records()[-1]
        self.assertEqual((rec["tax_rate_pct"], rec["tax_source"], rec["tax_result_code"], rec["tax_cents"]), ("9.2", "fallback_higher", 5, 870))
        self.assertIn("8.2% < SHOP_TAX_RATES zip 98359 9.2%", rec["tax_reason"])
        self.assertEqual(self.capture().status_code, 200)
        self.assertEqual((self.log_records()[-1]["state"], self.log_records()[-1]["amounts_match"]), ("created", True))

    def test_tax_rounding(self):
        import shop
        from decimal import Decimal
        self.assertEqual(shop.tax_cents(8985, 475, Decimal("9.2")), 870)     # 870.32
        self.assertEqual(shop.tax_cents(2995, 475, Decimal("10.55")), 366)   # 366.085 -> half up
        self.assertEqual(shop.tax_cents(5, 0, Decimal("9.2")), 0)
        self.assertEqual(shop.tax_cents(8985, 475, 0), 0)
        self.assertEqual(shop.fmt_pct(Decimal(".100") * 100), "10")


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
        self.assertEqual(body["application_context"]["brand_name"], "We Elect (Brunner Creative LLC)")
        pu = body["purchase_units"][0]
        self.assertEqual(pu["custom_id"], "weelect-shop")
        self.assertEqual([(i["sku"], i["quantity"], i["unit_amount"]["value"]) for i in pu["items"]],
                         [("tee.heather-navy.M", "2", "29.95"), ("cap.black.One size", "1", "29.95")], "client-sent prices ignored")
        bd = pu["amount"]["breakdown"]
        self.assertEqual((bd["item_total"]["value"], bd["shipping"]["value"], bd["tax_total"]["value"]), ("89.85", "4.75", "0.00"))
        self.assertEqual(pu["amount"]["value"], "94.60")
        self.assertEqual(pu["shipping"]["address"], {"address_line_1": "1 Main St", "admin_area_2": "Austin", "admin_area_1": "TX",
                                                     "postal_code": "78701", "country_code": "US"})
        self.assertEqual(pu["shipping"]["name"]["full_name"], "Pat Reader")
        tok = [c for c in self.fake.calls if c[1].endswith("/v1/oauth2/token")][0]
        self.assertEqual(tok[2]["auth"], ("AcidUnitTest", "EsecretUnitTest"))

    def test_breakdown_adds_up_with_wa_tax(self):
        from decimal import Decimal
        self.fake.dor = (200, DOR_SEATTLE)
        seattle = dict(WA_ADDR, line1="601 Union St", city="Seattle", zip="98101")
        r = self.create(CART, seattle)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["total"], "$104.58")
        pu = json.loads(self.fake.paypal_calls("/v2/checkout/orders")[0][2]["data"])["purchase_units"][0]
        bd = {k: v["value"] for k, v in pu["amount"]["breakdown"].items()}
        self.assertEqual(bd, {"item_total": "89.85", "shipping": "4.75", "tax_total": "9.98"})   # 94.60 * 10.55% = 9.9803
        self.assertEqual(pu["amount"]["value"], "104.58")
        self.assertEqual(sum(Decimal(v) for v in bd.values()), Decimal(pu["amount"]["value"]), "item_total + shipping + tax_total = total")
        items = sum(Decimal(i["unit_amount"]["value"]) * int(i["quantity"]) for i in pu["items"])
        self.assertEqual(items, Decimal(bd["item_total"]))
        self.assertEqual(pu["description"], "We Elect Gifts order. WA sales tax 10.55% (DOR location 1726)")
        rec = self.log_records()[-1]
        self.assertEqual((rec["state"], rec["tax_cents"], rec["tax_rate_pct"], rec["tax_loccode"], rec["tax_source"], rec["taxable_cents"], rec["tax_period"]),
                         ("opened", 998, "10.55", "1726", "dor", 9460, "Q42026"))
        self.assertNotIn("Union", json.dumps(self.log_records()), "no addresses in the log")

    def test_breakdown_math_many_carts(self):
        """item_total + shipping + tax_total always equals the order total, to the cent, for WA and non-WA."""
        from decimal import Decimal
        import shop
        carts = [[CART[0]], [CART[1]], CART, [dict(CART[0], q=10)], [dict(CART[0], q=3), dict(CART[1], q=7)]]
        for addr in (WA_ADDR, ADDR):
            for items in carts:
                lines, err = shop.parse_cart(items)
                self.assertIsNone(err)
                body, q = shop.build_paypal_order(lines, addr)
                amt = body["purchase_units"][0]["amount"]
                bd = {k: Decimal(v["value"]) for k, v in amt["breakdown"].items()}
                self.assertEqual(bd["item_total"] + bd["shipping"] + bd["tax_total"], Decimal(amt["value"]), (addr["state"], items))
                want = ((bd["item_total"] + bd["shipping"]) * Decimal("0.092")).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP") if addr["state"] == "WA" else Decimal("0.00")
                self.assertEqual(bd["tax_total"], want)

    def test_wa_order_records_tax_for_filing_after_capture(self):
        self.assertEqual(self.create(CART, WA_ADDR).status_code, 200)
        self.assertEqual(self.capture().status_code, 200)
        url, payload = self.fake.pf_posts[0]
        self.assertEqual(payload["retail_costs"]["tax"], "8.70")
        rec = self.log_records()[-1]
        self.assertEqual((rec["state"], rec["amounts_match"], rec["tax_cents"], rec["tax_rate_pct"], rec["tax_loccode"], rec["taxable_cents"], rec["ship_state"]),
                         ("created", True, 870, "9.2", "1800", 9460, "WA"))

    def test_capture_keeps_rate_charged_even_if_dor_changes(self):
        import wa_tax, requests
        self.assertEqual(self.create(CART, WA_ADDR).status_code, 200)
        wa_tax.reset()
        self.fake.dor = requests.ConnectionError("down")     # fulfilment never depends on DOR: the rate rides on the PayPal order
        self.assertEqual(self.capture().status_code, 200)
        rec = self.log_records()[-1]
        self.assertEqual((rec["state"], rec["amounts_match"], rec["tax_cents"], rec["tax_loccode"]), ("created", True, 870, "1800"))

    def test_live_env_uses_live_api(self):
        with mock.patch.dict(os.environ, {"PAYPAL_ENV": "live"}):
            self.create()
        self.assertTrue(any(c[1] == "https://api-m.paypal.com/v2/checkout/orders" for c in self.fake.calls))

    def test_rejects_bad_carts_and_addresses(self):
        bad = [[], "x", [{"p": "mug", "c": "navy", "s": "M", "q": 1}], [{"p": "tee", "c": "pink", "s": "M", "q": 1}],
               [{"p": "tee", "c": "dark-grey", "s": "3XL", "q": 1}],
               [{"p": "tee", "c": "heather-navy", "s": "XXXL", "q": 1}], [{"p": "tee", "c": "heather-navy", "s": "M", "q": 0}],
               [{"p": "tee", "c": "heather-navy", "s": "M", "q": 11}], [{"p": "tee", "c": "heather-navy", "s": "M", "q": "2"}],
               [{"p": "tee", "c": "heather-navy", "s": "M", "q": True}], [{"p": "tee", "c": "heather-navy", "s": "M", "q": 6}, {"p": "tee", "c": "heather-navy", "s": "M", "q": 6}]]
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
        q = self.api("/shop/quote", {"items": CART, "address": ADDR}).get_json()
        self.assertEqual((q["items"], q["shipping"], q["tax"], q["total"]), ("$89.85", "$4.75", "$0.00", "$94.60"))
        q2 = self.api("/shop/quote", {"items": CART, "address": dict(ADDR, state="OR", zip="97201")}).get_json()
        self.assertEqual((q2["tax"], q2["total"]), ("$0.00", "$94.60"))
        self.assertEqual([c for c in self.fake.calls if "paypal" in c[1]], [], "quotes never call PayPal")
        rates = [c for c in self.fake.calls if "shipping/rates" in c[1]]
        self.assertEqual(len(rates), 2, "one Printful rate lookup per destination, cached")
        body = json.loads(rates[0][2]["data"])
        self.assertEqual(sorted((i["variant_id"], i["quantity"]) for i in body["items"]), sorted([(TEE_NAVY_M[0], 2), (CAP[0], 1)]))
        self.assertEqual((body["recipient"]["state_code"], body["recipient"]["zip"]), ("TX", "78701"))

    def test_printful_rates_down_means_no_quote_and_no_order(self):
        with mock.patch("shop.printful_shipping_rates", return_value=(None, "ConnectionError")):
            r = self.api("/shop/quote", {"items": CART, "address": ADDR})
            self.assertEqual(r.status_code, 503)
            self.assertIn("shipping rate", r.get_json()["error"])
            self.assertEqual(self.create().status_code, 503)
        self.assertEqual(self.fake.paypal_calls("/v2/checkout/orders"), [])


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
        items = {i["sync_variant_id"]: i for i in payload["items"]}
        self.assertEqual(items[TEE_NAVY_M[1]]["quantity"], 2, "Heather Navy M tee, the store's own synced variant")
        self.assertEqual(items[CAP[1]]["quantity"], 1, "washed cap")
        self.assertEqual(items[TEE_NAVY_M[1]]["retail_price"], "29.95")
        self.assertNotIn("files", items[TEE_NAVY_M[1]], "print files come from the Printful store product")
        self.assertEqual(payload["retail_costs"], {"currency": "USD", "subtotal": "89.85", "shipping": "4.75", "tax": "0.00"})
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
            pu["amount"]["value"] = "34.71"
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
        self.create()
        with mock.patch.dict(os.environ, {"PRINTFUL_API_TOKEN": ""}):
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
        self.assertIn("We Elect Unisex t-shirt", t)
        self.assertIn("We Elect Washed Cap", t)
        self.assertIn("Large organic tote bag", t)
        self.assertIn("$29.95", t)
        self.assertIn("$34.95", t)
        self.assertIn("We Elect Copper Vacuum Insulated Bottle", t)
        self.assertIn("https://files.cdn.printful.com", r.headers["Content-Security-Policy"])
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
        self.assertIn("sold by Brunner Creative LLC, which publishes We Elect and is the seller of record", t)

    def test_checkout_page(self):
        self.assertIn("sold by Brunner Creative LLC", self.c.get("/shop/checkout", base_url=self.base).get_data(as_text=True))
        r = self.c.get("/shop/checkout", base_url=self.base)
        t = r.get_data(as_text=True)
        self.assertIn('data-client-id="AcidUnitTest"', t)
        self.assertNotIn("EsecretUnitTest", t)
        self.assertIn('<option value="TX">Texas</option>', t)
        self.assertIn("shop-checkout.js", t)
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_tax_explained_in_fine_print(self):
        t = self.c.get("/support").get_data(as_text=True)
        self.assertIn("Sales tax is added at checkout for orders shipped to Washington", t)
        self.assertIn("orders shipped to other states pay no sales tax", t)

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
        """The committed snapshot (the fallback when Printful is down) is a well-formed copy of the Printful store."""
        import shop, printful_store
        cat = printful_store._read_json(printful_store.SNAPSHOT_PATH)
        self.assertEqual(cat["source"], "printful")
        self.assertEqual(sorted(p["id"] for p in cat["products"]), ["bottle", "cap", "tee", "tote"])
        for p in cat["products"]:
            self.assertTrue(p["printful_product_id"])
            for c in p["colors"]:
                self.assertTrue(os.path.exists(os.path.join(os.path.dirname(shop.__file__), c["image"].lstrip("/"))), c["image"])
            for key, v in p["variants"].items():
                self.assertIsInstance(v["printful_variant_id"], int)
                self.assertIsInstance(v["sync_variant_id"], int)
                self.assertGreater(v["price_cents"], 0)
                self.assertLessEqual(len(f"{p['id']}.{key.replace(':', '.')}"), 127, "fits PayPal's sku limit")
        tee = next(p for p in cat["products"] if p["id"] == "tee")
        self.assertEqual(len(tee["variants"]), 30)
        self.assertNotIn("dark-grey:3XL", tee["variants"])

    def test_printful_shipping_rates(self):
        import shop
        rates, err = shop.printful_shipping_rates({"country_code": "US"}, [{"variant_id": 8510, "quantity": 1}])
        self.assertEqual(rates[0]["rate"], "4.75")
        with mock.patch("shop.requests.post", side_effect=shop.requests.ConnectionError("x")):
            rates, err = shop.printful_shipping_rates({"country_code": "US"}, [])
        self.assertIsNone(rates)
        self.assertEqual(err, "ConnectionError")



# ------------------------------------------------------------------ one-time gifts (PayPal, no Printful)
class DonationTests(ShopBase):
    def test_amount_validation(self):
        import shop
        for raw, want in [("25", 2500), ("25.5", 2550), ("$1,000.00", 100000), ("1", 100), (10, 1000), ("10000", 1000000)]:
            self.assertEqual(shop.parse_donation(raw), (want, None), raw)
        for raw in [None, True, "", "abc", "0.99", "0", "-5", "10000.01", "25.555", "1e3", "12,34", 5.5, [], "100000000"]:
            self.assertIsNone(shop.parse_donation(raw)[0], raw)

    def test_gift_order_has_no_shipping_tax_or_printful(self):
        with mock.patch.dict(os.environ, {"SHOP_TAX_RATES": '{"WA": 9.2}'}):
            r = self.api("/shop/paypal/donation", {"amount": "25.00", "price": "0.01"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json(), {"id": ORDER_ID, "total": "$25.00"})
        body = json.loads(self.fake.paypal_calls("/v2/checkout/orders")[0][2]["data"])
        pu = body["purchase_units"][0]
        self.assertEqual(pu["custom_id"], "weelect-gift")
        self.assertEqual(pu["amount"], {"currency_code": "USD", "value": "25.00", "breakdown": {"item_total": {"currency_code": "USD", "value": "25.00"}}})
        self.assertEqual(pu["items"][0]["category"], "DONATION")
        self.assertIn("Not tax-deductible", pu["description"])
        self.assertNotIn("shipping", pu)
        self.assertEqual(body["application_context"]["shipping_preference"], "NO_SHIPPING")
        self.assertEqual([c for c in self.fake.calls if "printful" in c[1]], [])
        self.assertEqual(self.fake.dor_calls(), [], "gifts are never taxed and never look up a rate")
        self.assertNotIn("tax_total", pu["amount"]["breakdown"])

    def test_bad_amount_and_csrf(self):
        self.assertEqual(self.api("/shop/paypal/donation", {"amount": "0.50"}).status_code, 400)
        self.assertEqual(self.api("/shop/paypal/donation", {"amount": "25"}, token=None).status_code, 403)
        self.assertEqual(self.fake.paypal_calls("/v2/checkout/orders"), [])

    def test_capture_gift_logs_and_never_touches_printful(self):
        self.assertEqual(self.api("/shop/paypal/donation", {"amount": "50"}).status_code, 200)
        r = self.capture()
        self.assertEqual(r.get_json(), {"status": "completed", "redirect": "/shop/success?order=" + ORDER_ID})
        self.assertEqual(self.fake.pf_posts, [])
        rec = self.log_records()[-1]
        self.assertEqual((rec["kind"], rec["state"], rec["paid_cents"]), ("gift", "gift_completed", 5000))
        self.assertEqual(self.webhook(capture_event()).status_code, 200)
        self.assertEqual(self.fake.pf_posts, [], "webhook ignores gifts for fulfilment")
        t = self.c.get("/shop/success?order=" + ORDER_ID).get_data(as_text=True)
        self.assertIn("Your gift of <strong>$50.00</strong> came through", t)
        self.assertIn("not a tax-deductible charitable contribution", t)
        self.assertIn("Brunner Creative LLC", t)
        self.assertIn("3C679366HH908993F", t)

    def test_support_page_gift_form(self):
        t = self.c.get("/support", base_url=self.base).get_data(as_text=True)
        self.assertIn('id="gift-form"', t)
        self.assertIn('data-client-id="AcidUnitTest"', t)
        self.assertIn("not a tax-deductible charitable contribution", t)
        self.assertIn("501(c)(3)", t)
        self.assertIn("donate.js", t)
        self.assertNotIn("Give through Ko-fi", t)
        for a in ("$5", "$10", "$25", "$50"):
            self.assertIn(f">{a}</button>", t)


class ShopClosedDonationsOpenTests(ShopBase):
    """Launch state: SHOP_OPEN unset, PayPal client id + secret set. Gifts work; the store is a preview and refuses orders."""
    extra_env = {"SHOP_OPEN": "", "PRINTFUL_API_TOKEN": "", "PAYPAL_WEBHOOK_ID": ""}

    def csrf(self):
        r = self.c.get("/support", base_url=self.base)
        self.assertEqual(r.status_code, 200)
        return re.search(r'name="csrf-token" content="([^"]+)"', r.get_data(as_text=True)).group(1)

    def test_page_shows_gift_form_and_store_preview(self):
        import shop
        self.assertFalse(shop.shop_open())
        self.assertTrue(shop.donations_open())
        t = self.c.get("/support", base_url=self.base).get_data(as_text=True)
        self.assertIn('id="gift-form"', t)
        self.assertIn("donate.js", t)
        self.assertNotIn("Give through Ko-fi", t)
        self.assertIn("Coming soon", t)
        self.assertIn("We Elect Unisex t-shirt", t)
        self.assertIn("$34.95", t)
        for frag in ("add-btn", "Add to cart", "cart-checkout", 'id="shop-data"', "shop.js"):
            self.assertNotIn(frag, t)
        self.assertIn("we do not accept returns or exchanges", t)
        self.assertIn("we will replace it", t)

    def test_store_endpoints_refuse_server_side(self):
        tok = self.csrf()
        h = {"X-CSRF-Token": tok}
        self.assertEqual(self.c.get("/shop/checkout", base_url=self.base).status_code, 302)
        self.assertEqual(self.c.post("/shop/quote", json={"items": CART, "address": ADDR}, headers=h, base_url=self.base).status_code, 503)
        self.assertEqual(self.c.post("/shop/paypal/order", json={"items": CART, "address": ADDR}, headers=h, base_url=self.base).status_code, 503)
        self.assertEqual(self.fake.paypal_calls("/v2/checkout/orders"), [])
        self.assertEqual([c for c in self.fake.calls if "printful" in c[1]], [])

    def test_store_order_is_never_captured_while_closed(self):
        # a store order that somehow exists at PayPal (e.g. created before the store was closed)
        self.fake.orders[ORDER_ID] = {"id": ORDER_ID, "status": "APPROVED", "purchase_units": [{"custom_id": "weelect-shop", "amount": {"currency_code": "USD", "value": "10.00"}}]}
        r = self.capture()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.fake.paypal_calls("/capture"), [])

    def test_gift_end_to_end_without_printful_or_webhook(self):
        r = self.api("/shop/paypal/donation", {"amount": "10.50"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["total"], "$10.50")
        r = self.capture()
        self.assertEqual(r.get_json(), {"status": "completed", "redirect": "/shop/success?order=" + ORDER_ID})
        rec = self.log_records()[-1]
        self.assertEqual((rec["kind"], rec["state"], rec["paid_cents"]), ("gift", "gift_completed", 1050))
        self.assertEqual([c for c in self.fake.calls if "printful" in c[1]], [])
        t = self.c.get("/shop/success?order=" + ORDER_ID, base_url=self.base).get_data(as_text=True)
        self.assertIn("Your gift of <strong>$10.50</strong> came through", t)
        self.assertEqual(self.c.post("/paypal/webhook", data=b"{}", base_url=self.base).status_code, 404, "no webhook id: endpoint off")


class ShopClosedWebhookTests(ShopBase):
    """Launch state with the (recommended) webhook id set: the webhook backs up gifts but never captures store orders."""
    extra_env = {"SHOP_OPEN": "", "PRINTFUL_API_TOKEN": ""}

    def csrf(self):
        return re.search(r'name="csrf-token" content="([^"]+)"', self.c.get("/support", base_url=self.base).get_data(as_text=True)).group(1)

    def test_approved_gift_captured_and_logged_once(self):
        self.assertEqual(self.api("/shop/paypal/donation", {"amount": "25"}).status_code, 200)
        ev = {"id": "WH-EVT-2", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": dict(self.fake.orders[ORDER_ID])}
        r = self.webhook(ev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["note"], "gift completed")
        self.assertEqual(self.webhook(ev).get_json()["note"], "duplicate")
        recs = [x for x in self.log_records() if x.get("kind") == "gift"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["paid_cents"], 2500)

    def test_approved_store_order_ignored(self):
        self.fake.orders[ORDER_ID] = {"id": ORDER_ID, "status": "APPROVED", "purchase_units": [{"custom_id": "weelect-shop", "amount": {"currency_code": "USD", "value": "10.00"}}]}
        r = self.webhook({"id": "WH-EVT-3", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": dict(self.fake.orders[ORDER_ID])})
        self.assertEqual(r.get_json()["note"], "ignored: store closed")
        self.assertEqual(self.fake.paypal_calls("/capture"), [])

    def test_shop_open_flag_values(self):
        import shop
        self.assertFalse(shop.shop_open())
        for v in ("0", "no", "off", ""):
            with mock.patch.dict(os.environ, {"SHOP_OPEN": v}):
                self.assertFalse(shop.shop_open(), v)
        with mock.patch.dict(os.environ, {"SHOP_OPEN": "1"}):
            self.assertTrue(shop.shop_open())
            with mock.patch.dict(os.environ, {"PAYPAL_WEBHOOK_ID": ""}):
                self.assertFalse(shop.shop_open(), "store still needs the webhook id")


class GiftsClosedTests(unittest.TestCase):
    def test_without_paypal_page_falls_back_to_kofi_and_endpoint_closed(self):
        env = dict(TEST_ENV, PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="", PAYPAL_WEBHOOK_ID="")
        with mock.patch.dict(os.environ, env):
            import app as appmod, printful_store
            printful_store.reset()
            appmod._HITS.clear()
            c = appmod.app.test_client()
            t = c.get("/support").get_data(as_text=True)
            self.assertIn("Give through Ko-fi", t)
            self.assertNotIn('id="gift-form"', t)
            self.assertEqual(c.post("/shop/paypal/donation", json={"amount": "5"}).status_code, 503)


# ------------------------------------------------------------------ live Printful catalog
def _pf_detail(pid, name, variants, thumb="https://files.cdn.printful.com/files/x/thumb_preview.png"):
    return {"sync_product": {"id": pid, "name": name, "thumbnail_url": thumb, "is_ignored": False},
            "sync_variants": [{"id": sv, "variant_id": cv, "color": col, "size": size, "retail_price": price, "currency": "USD",
                               "availability_status": "active", "is_ignored": False, "product": {"variant_id": cv, "product_id": 71},
                               "files": [{"type": "preview", "preview_url": f"https://files.cdn.printful.com/files/x/{col}_preview.png"}]}
                              for sv, cv, col, size, price in variants]}


class FakePF:
    def __init__(self, down=False):
        self.down, self.calls = down, []
        self.details = {
            1: _pf_detail(1, "We Elect Unisex t-shirt", [(11, 4011, "Maroon", "L", "29.95"), (12, 4012, "Maroon", "S", "29.95"),
                                                         (13, 4013, "Maroon", "3XL", "31.95"), (14, 4014, "Dark Grey", "S", "29.95")]),
            2: _pf_detail(2, "Large organic tote bag", [(21, 5021, "Black", "One size", "33.95")])}

    def get(self, url, **kw):
        self.calls.append(url)
        if self.down:
            raise __import__("requests").ConnectionError("down")
        if "/store/products?" in url:
            return Resp(200, {"result": [{"id": 1}, {"id": 2}], "paging": {"total": 2}})
        if "/store/products/" in url:
            return Resp(200, {"result": self.details[int(url.rsplit("/", 1)[1])]})
        if url.endswith("/products/71"):
            return Resp(200, {"result": {"product": {"title": "Unisex Staple T-Shirt | Bella + Canvas 3001"},
                                         "variants": [{"id": 4011, "color_code": "#721d37"}, {"id": 4014, "color_code": "#2A2929"}]}})
        raise AssertionError(url)


class LiveCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.dict(os.environ, dict(TEST_ENV, SHOP_LIVE_CATALOG="1", SHOP_CATALOG_CACHE=os.path.join(self.tmp, "cat.json")))
        self.patch.start()
        import printful_store
        self.pfs = printful_store
        printful_store.reset()

    def tearDown(self):
        self.patch.stop()
        self.pfs.reset()

    def test_builds_products_from_printful(self):
        cat = self.pfs.fetch_from_printful(FakePF())
        by = {p["id"]: p for p in cat["products"]}
        self.assertEqual(set(by), {"tee", "tote"})
        tee = by["tee"]
        self.assertEqual(tee["name"], "We Elect Unisex t-shirt")
        self.assertEqual(tee["sizes"], ["S", "L", "3XL"], "sizes in size order")
        self.assertEqual([c["name"] for c in tee["colors"]], ["Maroon", "Dark Grey"])
        self.assertEqual(tee["colors"][0]["swatch"], "#721d37")
        self.assertEqual(tee["variants"]["maroon:3XL"], {"printful_variant_id": 4013, "sync_variant_id": 13, "price_cents": 3195})
        self.assertEqual((tee["price_cents"], tee["price_max_cents"]), (2995, 3195))
        self.assertNotIn("dark-grey:L", tee["variants"])
        self.assertTrue(tee["colors"][0]["image"].startswith("https://files.cdn.printful.com/"))
        self.assertEqual(by["tote"]["price_cents"], 3395)

    def test_never_blocks_and_falls_back(self):
        import threading
        # first call serves the committed snapshot at once and refreshes in the background
        with mock.patch.object(threading, "Thread") as T:
            d = self.pfs.get()
            self.assertEqual(self.pfs.source(), "snapshot")
            self.assertTrue(d["products"])
            T.assert_called_once()
        # Printful down: keep serving the snapshot, back off
        self.assertFalse(self.pfs.refresh(FakePF(down=True)))
        self.assertEqual(self.pfs.source(), "snapshot")
        self.assertGreater(self.pfs._STATE["next_try"], time.time())
        # Printful back: live catalog, saved to disk for the next restart
        self.assertTrue(self.pfs.refresh(FakePF()))
        self.assertEqual(self.pfs.source(), "printful")
        self.assertEqual({p["id"] for p in self.pfs.get()["products"]}, {"tee", "tote"})
        self.pfs.reset()
        with mock.patch.object(threading, "Thread"):
            self.pfs.get()
        self.assertEqual(self.pfs.source(), "disk")

    def test_fresh_catalog_does_not_refetch(self):
        import threading
        self.pfs.refresh(FakePF())
        with mock.patch.object(threading, "Thread") as T:
            for _ in range(5):
                self.pfs.get()
            T.assert_not_called()

if __name__ == "__main__":
    unittest.main()

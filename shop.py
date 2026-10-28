"""Support page + small store for weelect.org.

Self-contained: Stripe embedded Checkout takes the money (card data never touches this server), and a
Stripe webhook creates a Printful order. Everything is configured with environment variables and the
whole thing degrades to a "coming soon" panel when they are not set. See docs/SHOP_SETUP.md.

Env vars (all optional; nothing is required for the rest of the site to work):
  STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET   Stripe (test keys sk_test_/pk_test_ first)
  PRINTFUL_API_TOKEN, PRINTFUL_STORE_ID                              Printful (store id only for account-level tokens)
  PRINTFUL_AUTO_CONFIRM=1     submit Printful orders for fulfillment automatically (default: leave as drafts)
  STRIPE_TAX=1                turn on Stripe Tax (automatic_tax). STRIPE_TAX_RATE_ID=txr_... applies one fixed rate instead
  STRIPE_API_VERSION, STRIPE_UI_MODE   override the pinned Stripe API version / ui_mode value
  SHOP_SECRET                 signing key for CSRF tokens (defaults to a hash of STRIPE_SECRET_KEY)
  SHOP_ORDER_LOG              path of the JSONL order log (default /tmp/weelect_shop_orders.jsonl)
  SHOP_CSP_REPORT_ONLY=1      send the shop pages' CSP as report-only (debugging)
  SELLER_NAME                 name shown as the seller (default "Brunner Creative")
"""
import hashlib, hmac, json, os, re, secrets, threading, time, logging
from urllib.parse import urlencode
import requests
from flask import Blueprint, request, jsonify, render_template, redirect, abort, make_response

ROOT = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(ROOT, "data", "shop_products.json")
log = logging.getLogger("weelect.shop")

STRIPE_API = "https://api.stripe.com/v1"
PRINTFUL_API = "https://api.printful.com"
WEBHOOK_TOLERANCE = 300          # seconds a Stripe signature timestamp may differ from now
CSRF_TTL = 4 * 3600
HTTP_TIMEOUT = 15
SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")
KOFI_PAGE = "https://ko-fi.com/weelect"
KOFI_EMBED = "https://ko-fi.com/weelect/?hidefeed=true&widget=true&embed=true&preview=true"

bp = Blueprint("shop", __name__)
_rate_limited = lambda bucket, limit, window=60: False   # replaced by register()
_error_page = None
_LOCK = threading.Lock()


# ------------------------------------------------------------------ config (read at call time so tests can patch env)
def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def site_url():
    return env("SITE_URL", "https://weelect.org").rstrip("/")


def stripe_ready():
    return bool(env("STRIPE_SECRET_KEY")) and bool(env("STRIPE_PUBLISHABLE_KEY"))


def webhook_ready():
    return bool(env("STRIPE_WEBHOOK_SECRET"))


def printful_ready():
    return bool(env("PRINTFUL_API_TOKEN"))


def shop_open():
    """The store takes orders only when Stripe checkout AND the webhook secret are set; otherwise 'coming soon'."""
    return stripe_ready() and webhook_ready() and bool(catalog_products())


def tax_mode():
    if env("STRIPE_TAX") == "1":
        return "stripe_tax"
    if env("STRIPE_TAX_RATE_ID"):
        return "fixed_rate"
    return "none"


# ------------------------------------------------------------------ catalog
_CAT = {"mtime": None, "data": None}


def catalog():
    try:
        m = os.path.getmtime(CATALOG_PATH)
    except OSError:
        return {"products": [], "limits": {}, "shipping": {}}
    if _CAT["mtime"] != m:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            _CAT["data"] = json.load(f)
        _CAT["mtime"] = m
    return _CAT["data"]


def variant_ok(p, color_id, size):
    v = (p.get("variants") or {}).get(f"{color_id}:{size}")
    return bool(v and v.get("printful_variant_id"))


def catalog_products():
    """Products that are switched on and have at least one orderable (variant id filled in) variant."""
    out = []
    for p in catalog().get("products", []):
        if p.get("available") and any(variant_ok(p, c["id"], s) for c in p["colors"] for s in p["sizes"]):
            out.append(p)
    return out


def find_product(pid):
    for p in catalog_products():
        if p["id"] == pid:
            return p
    return None


def money(cents):
    return f"${cents/100:,.2f}"


def parse_cart(raw):
    """Validate a client cart [{p,c,s,q}] against the catalog. Prices always come from the catalog.
    Returns (lines, error_message). Each line is a dict with product/variant info and a server-side price."""
    lim = catalog().get("limits", {})
    max_lines, max_qty = int(lim.get("max_lines", 8)), int(lim.get("max_qty_per_line", 10))
    if not isinstance(raw, list) or not raw:
        return None, "Your cart is empty."
    if len(raw) > max_lines:
        return None, "That cart has too many different items."
    merged, order = {}, []
    for it in raw:
        if not isinstance(it, dict):
            return None, "That cart could not be read."
        pid, cid, size, q = it.get("p"), it.get("c"), it.get("s"), it.get("q")
        if not all(isinstance(x, str) for x in (pid, cid, size)) or isinstance(q, bool) or not isinstance(q, int):
            return None, "That cart could not be read."
        p = find_product(pid)
        if not p or not any(c["id"] == cid for c in p["colors"]) or size not in p["sizes"] or not variant_ok(p, cid, size):
            return None, "One of those items is not available."
        if q < 1 or q > max_qty:
            return None, f"Please order between 1 and {max_qty} of each item."
        key = (pid, cid, size)
        if key not in merged:
            merged[key] = 0
            order.append(key)
        merged[key] += q
        if merged[key] > max_qty:
            return None, f"Please order between 1 and {max_qty} of each item."
    lines = []
    for pid, cid, size in order:
        p = find_product(pid)
        color = next(c for c in p["colors"] if c["id"] == cid)
        label = f"{p['name']} ({color['name']}" + ("" if size == "One size" else f", {size}") + ")"
        lines.append({"p": pid, "c": cid, "s": size, "q": merged[(pid, cid, size)], "name": label, "price_cents": int(p["price_cents"]),
                      "tax_code": p.get("tax_code"), "variant_id": p["variants"][f"{cid}:{size}"]["printful_variant_id"],
                      "file_type": (p.get("printful") or {}).get("file_type", "default"),
                      "file_url": ((p.get("printful") or {}).get("files") or {}).get(cid)})
    return lines, None


def subtotal_cents(lines):
    return sum(l["price_cents"] * l["q"] for l in lines)


def shipping_cents(lines):
    sh = catalog().get("shipping", {})
    n = sum(l["q"] for l in lines)
    return int(sh.get("first_item_cents", 0)) + int(sh.get("additional_item_cents", 0)) * max(0, n - 1)


def cart_token(lines):
    """Compact cart for Stripe metadata (500 char limit per value): 'tee.navy.M:2,hat.navy.One size:1'."""
    return ",".join(f"{l['p']}.{l['c']}.{l['s']}:{l['q']}" for l in lines)


def cart_from_token(tok):
    raw = []
    for part in (tok or "").split(","):
        try:
            key, q = part.rsplit(":", 1)
            pid, cid, size = key.split(".", 2)
            raw.append({"p": pid, "c": cid, "s": size, "q": int(q)})
        except ValueError:
            return None, "bad cart token"
    return parse_cart(raw)


# ------------------------------------------------------------------ CSRF (signed token + double-submit cookie + Origin check)
def _csrf_key():
    k = env("SHOP_SECRET") or env("STRIPE_SECRET_KEY")
    if not k:
        return None
    return hashlib.sha256(("weelect-csrf|" + k).encode()).digest()


def make_csrf():
    key = _csrf_key()
    if not key:
        return None
    body = f"{int(time.time())}.{secrets.token_urlsafe(18)}"
    return body + "." + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def check_csrf(token, cookie):
    key = _csrf_key()
    if not key or not token or not cookie or not hmac.compare_digest(token, cookie):
        return False
    try:
        ts, nonce, sig = token.split(".")
        ts_i = int(ts)
    except ValueError:
        return False
    good = hmac.new(key, f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and 0 <= time.time() - ts_i <= CSRF_TTL


def origin_ok():
    o = request.headers.get("Origin")
    if not o:
        return True   # same-origin form/fetch from older browsers; the token + cookie check still applies
    return o.rstrip("/") in {site_url(), request.host_url.rstrip("/")}


def set_csrf_cookie(resp, token):
    resp.set_cookie("we_csrf", token, max_age=CSRF_TTL, path="/shop", httponly=True, samesite="Strict",
                    secure=site_url().startswith("https://"))
    return resp


# ------------------------------------------------------------------ security headers for shop pages
STRIPE_JS = "https://js.stripe.com https://*.js.stripe.com"


def shop_csp():
    return "; ".join([
        "default-src 'self'",
        # The site uses a few small inline scripts/styles, so 'unsafe-inline' stays; Stripe.js must come from js.stripe.com.
        f"script-src 'self' 'unsafe-inline' {STRIPE_JS} https://checkout.stripe.com",
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self'",
        "img-src 'self' data: https://*.stripe.com https://ko-fi.com https://storage.ko-fi.com",
        "connect-src 'self' https://api.stripe.com https://checkout.stripe.com https://*.stripe.com",
        f"frame-src {STRIPE_JS} https://hooks.stripe.com https://checkout.stripe.com https://ko-fi.com",
        "frame-ancestors 'none'", "base-uri 'self'", "object-src 'none'",
        # Embedded Checkout posts nothing to Stripe from our page, so forms may only go to us.
        "form-action 'self'",
    ])


def shop_headers(resp, payment=True):
    name = "Content-Security-Policy-Report-Only" if env("SHOP_CSP_REPORT_ONLY") == "1" else "Content-Security-Policy"
    resp.headers[name] = shop_csp()
    if payment:   # lets Apple Pay / Google Pay work inside Stripe's iframe (the site-wide default is payment=())
        resp.headers["Permissions-Policy"] = ('geolocation=(), camera=(), microphone=(), '
                                              'payment=(self "https://js.stripe.com" "https://checkout.stripe.com" "https://ko-fi.com")')
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ Stripe (plain REST via requests; no SDK)
def _flatten(prefix, val, out):
    if isinstance(val, dict):
        for k, v in val.items():
            _flatten(f"{prefix}[{k}]" if prefix else k, v, out)
    elif isinstance(val, (list, tuple)):
        for i, v in enumerate(val):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(val, bool):
        out.append((prefix, "true" if val else "false"))
    elif val is not None:
        out.append((prefix, str(val)))
    return out


def stripe_headers(idem=None):
    h = {"Stripe-Version": env("STRIPE_API_VERSION", "2026-03-25.dahlia")}
    if idem:
        h["Idempotency-Key"] = idem
    return h


class ShopError(Exception):
    """Raised for failures we log in detail but never show to visitors."""


def stripe_request(method, path, params=None, idem=None):
    try:
        r = requests.request(method, STRIPE_API + path, auth=(env("STRIPE_SECRET_KEY"), ""), headers=stripe_headers(idem),
                             data=_flatten("", params or {}, []) if method == "POST" else None, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ShopError(f"stripe network error: {type(e).__name__}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        raise ShopError(f"stripe {r.status_code} {err.get('type')} {err.get('code')} {err.get('param')}")
    return body


def build_checkout_session_params(lines):
    mode = tax_mode()
    items = []
    for l in lines:
        pd = {"currency": catalog().get("currency", "usd"), "unit_amount": l["price_cents"], "product_data": {"name": l["name"]}}
        code = l["tax_code"] or env("STRIPE_TAX_CODE") or None
        if mode == "stripe_tax":
            pd["tax_behavior"] = "exclusive"
            if code:
                pd["product_data"]["tax_code"] = code
        item = {"price_data": pd, "quantity": l["q"]}
        if mode == "fixed_rate":
            item["tax_rates"] = [env("STRIPE_TAX_RATE_ID")]
        items.append(item)
    sh = catalog().get("shipping", {})
    rate = {"type": "fixed_amount", "display_name": sh.get("label", "Standard shipping"),
            "fixed_amount": {"amount": shipping_cents(lines), "currency": catalog().get("currency", "usd")}}
    if mode == "stripe_tax":
        rate["tax_behavior"] = "exclusive"
    params = {
        "mode": "payment",
        "ui_mode": env("STRIPE_UI_MODE", "embedded_page"),
        "return_url": site_url() + "/shop/success?session_id={CHECKOUT_SESSION_ID}",
        "line_items": items,
        "shipping_address_collection": {"allowed_countries": sh.get("countries", ["US"])},
        "shipping_options": [{"shipping_rate_data": rate}],
        "billing_address_collection": "auto",
        "phone_number_collection": {"enabled": False},
        "submit_type": "pay",
        "metadata": {"source": "weelect-shop", "cart": cart_token(lines), "subtotal_cents": subtotal_cents(lines)},
    }
    if mode == "stripe_tax":
        params["automatic_tax"] = {"enabled": True}
    return params


def verify_stripe_signature(payload, header, secret, now=None, tolerance=WEBHOOK_TOLERANCE):
    """Stripe-Signature: 't=1700000000,v1=hex[,v1=hex]'; signed payload is f'{t}.{raw body}' (HMAC-SHA256)."""
    if not header or not secret:
        return False
    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sigs.append(v)
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return False
    if not sigs or abs((now if now is not None else time.time()) - ts_i) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


# ------------------------------------------------------------------ Printful
def printful_headers():
    h = {"Authorization": "Bearer " + env("PRINTFUL_API_TOKEN"), "Content-Type": "application/json"}
    if env("PRINTFUL_STORE_ID"):
        h["X-PF-Store-Id"] = env("PRINTFUL_STORE_ID")
    return h


def external_id(session_id):
    """Printful external ids are unique per store (<=32 chars), which makes order creation idempotent across restarts."""
    return "we" + hashlib.sha256(session_id.encode()).hexdigest()[:30]


def session_shipping(session):
    """Stripe moved shipping_details under collected_information in newer API versions; accept both."""
    sd = session.get("shipping_details") or ((session.get("collected_information") or {}).get("shipping_details")) or {}
    return sd


def build_printful_order(session, lines):
    sd = session_shipping(session)
    addr = sd.get("address") or {}
    cust = session.get("customer_details") or {}
    recipient = {"name": sd.get("name") or cust.get("name") or "Customer", "address1": addr.get("line1"), "address2": addr.get("line2") or "",
                 "city": addr.get("city"), "state_code": addr.get("state"), "country_code": addr.get("country"), "zip": addr.get("postal_code")}
    if cust.get("email"):
        recipient["email"] = cust["email"]    # Printful sends the shipping/tracking notice; We Elect sends no email itself
    if cust.get("phone"):
        recipient["phone"] = cust["phone"]
    items = []
    for l in lines:
        it = {"variant_id": l["variant_id"], "quantity": l["q"], "name": l["name"], "retail_price": f"{l['price_cents']/100:.2f}"}
        if l.get("file_url"):
            it["files"] = [{"type": l["file_type"], "url": site_url() + l["file_url"]}]
        items.append(it)
    sh = catalog().get("shipping", {})
    return {"external_id": external_id(session["id"]), "shipping": "STANDARD", "recipient": recipient, "items": items,
            "retail_costs": {"currency": "USD", "subtotal": f"{subtotal_cents(lines)/100:.2f}",
                             "shipping": f"{(session.get('total_details') or {}).get('amount_shipping', shipping_cents(lines))/100:.2f}",
                             "tax": f"{(session.get('total_details') or {}).get('amount_tax', 0)/100:.2f}"}}


def printful_create_order(payload, confirm):
    url = PRINTFUL_API + "/orders" + ("?confirm=true" if confirm else "")
    try:
        r = requests.post(url, headers=printful_headers(), data=json.dumps(payload), timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return {"ok": False, "retry": True, "error": f"network {type(e).__name__}"}
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200 and isinstance(body.get("result"), dict):
        res = body["result"]
        return {"ok": True, "printful_id": res.get("id"), "status": res.get("status")}
    msg = str(((body.get("error") or {}).get("message")) or body.get("result") or "")[:200]
    if r.status_code in (429,) or r.status_code >= 500:
        return {"ok": False, "retry": True, "error": f"printful {r.status_code}"}
    if r.status_code == 400 and "external" in msg.lower() and ("exist" in msg.lower() or "unique" in msg.lower() or "already" in msg.lower()):
        return {"ok": True, "printful_id": None, "status": "duplicate", "duplicate": True}
    return {"ok": False, "retry": False, "error": f"printful {r.status_code} {msg}"}


def printful_shipping_rates(recipient, items):
    """Printful shipping rates for a recipient + [{variant_id, quantity}]. Returns (rates, error). Used by scripts/printful_sync.py
    to check the flat rates in data/shop_products.json; checkout itself uses the flat rates (see docs)."""
    try:
        r = requests.post(PRINTFUL_API + "/shipping/rates", headers=printful_headers(),
                          data=json.dumps({"recipient": recipient, "items": items, "currency": "USD"}), timeout=HTTP_TIMEOUT)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, f"printful {r.status_code}"
    return body.get("result") or [], None


# ------------------------------------------------------------------ order log + idempotency
def order_log_path():
    return env("SHOP_ORDER_LOG", "/tmp/weelect_shop_orders.jsonl")


def read_log():
    out = {}
    try:
        with open(order_log_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                out[rec.get("session_id")] = rec
    except OSError:
        pass
    return out


def write_log(rec):
    rec = dict(rec, ts=int(time.time()))
    line = json.dumps(rec, separators=(",", ":"), sort_keys=True)
    print("SHOP_ORDER " + line, flush=True)       # Render keeps stdout logs; the file is only a convenience (ephemeral disk)
    try:
        with open(order_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def handle_checkout_completed(session):
    """Returns (http_status, note). 5xx asks Stripe to retry later; everything else is final."""
    sid = session.get("id", "")
    if not SESSION_ID_RE.match(sid):
        return 200, "ignored: bad session id"
    if (session.get("metadata") or {}).get("source") != "weelect-shop":
        return 200, "ignored: not a shop session"
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        return 200, "ignored: not paid yet"
    with _LOCK:
        prev = read_log().get(sid)
        if prev and prev.get("state") in ("created", "failed_permanent"):
            return 200, "duplicate"
        lines, err = cart_from_token((session.get("metadata") or {}).get("cart"))
        if err:
            write_log({"session_id": sid, "state": "failed_permanent", "error": "cart: " + str(err)})
            return 200, "needs manual attention"
        if not printful_ready():
            write_log({"session_id": sid, "state": "failed_permanent", "error": "PRINTFUL_API_TOKEN not set; create order by hand",
                       "cart": cart_token(lines)})
            return 200, "needs manual attention"
        try:
            sub_ok = int(session.get("amount_subtotal", -1)) == subtotal_cents(lines)
        except (TypeError, ValueError):
            sub_ok = False
        payload = build_printful_order(session, lines)
        confirm = env("PRINTFUL_AUTO_CONFIRM") == "1" and sub_ok
        res = printful_create_order(payload, confirm)
        if res["ok"]:
            write_log({"session_id": sid, "state": "created", "printful_id": res.get("printful_id"), "printful_status": res.get("status"),
                       "confirmed": bool(confirm), "subtotal_matches": sub_ok, "cart": cart_token(lines),
                       "amount_total": session.get("amount_total")})
            return 200, "created"
        if res.get("retry"):
            write_log({"session_id": sid, "state": "retrying", "error": res["error"]})
            return 500, "retry"
        write_log({"session_id": sid, "state": "failed_permanent", "error": res["error"], "cart": cart_token(lines)})
        return 200, "needs manual attention"


# ------------------------------------------------------------------ routes
def _json_error(status, msg):
    return jsonify({"error": msg}), status


def _page_context():
    products = catalog_products()
    pub = []
    for p in products:
        pub.append({"id": p["id"], "name": p["name"], "kind": p["kind"], "blurb": p["blurb"], "details": p.get("details", []),
                    "price_cents": p["price_cents"], "price": money(p["price_cents"]), "colors": p["colors"],
                    "sizes": [s for s in p["sizes"] if any(variant_ok(p, c["id"], s) for c in p["colors"])],
                    "ok": {f"{c['id']}:{s}": variant_ok(p, c["id"], s) for c in p["colors"] for s in p["sizes"]}})
    sh = catalog().get("shipping", {})
    return {"products": pub, "shop_open": shop_open(), "kofi_page": KOFI_PAGE, "kofi_embed": KOFI_EMBED,
            "seller": env("SELLER_NAME", "Brunner Creative"), "tax_mode": tax_mode(),
            "shipping_label": sh.get("label", "Standard shipping"), "shipping_first": money(int(sh.get("first_item_cents", 0))),
            "shipping_more": money(int(sh.get("additional_item_cents", 0))),
            "shop_json": {"products": [{k: p[k] for k in ("id", "name", "price_cents", "colors", "ok")} for p in pub],
                          "shipping": {"first": int(sh.get("first_item_cents", 0)), "more": int(sh.get("additional_item_cents", 0)),
                                       "label": sh.get("label", "Standard shipping")},
                          "limits": catalog().get("limits", {})}}


@bp.route("/support")
def support():
    ctx = _page_context()
    token = make_csrf() if ctx["shop_open"] else None
    resp = make_response(render_template("support.html", csrf=token, **ctx))
    if token:
        set_csrf_cookie(resp, token)
    return shop_headers(resp)


@bp.route("/shop")
def shop_redirect():
    return redirect("/support#shop", code=302)


@bp.route("/shop/checkout", methods=["GET"])
def checkout_page():
    if not shop_open():
        return redirect("/support#shop", code=302)
    token = make_csrf()
    resp = make_response(render_template("shop_checkout.html", csrf=token, publishable_key=env("STRIPE_PUBLISHABLE_KEY"),
                                         **{k: v for k, v in _page_context().items() if k in ("seller", "tax_mode")}))
    set_csrf_cookie(resp, token)
    return shop_headers(resp)


@bp.route("/shop/checkout", methods=["POST"])
def create_checkout():
    if _rate_limited("shop-checkout", 12):
        return _json_error(429, "Too many requests. Please wait a minute and try again.")
    if not shop_open():
        return _json_error(503, "The store is not open yet.")
    if not origin_ok() or not check_csrf(request.headers.get("X-CSRF-Token", ""), request.cookies.get("we_csrf", "")):
        return _json_error(403, "Your session expired. Please reload the page and try again.")
    if request.content_length is not None and request.content_length > 4096:
        return _json_error(413, "That request was too large.")
    data = request.get_json(silent=True)
    lines, err = parse_cart(data.get("items") if isinstance(data, dict) else None)
    if err:
        return _json_error(400, err)
    params = build_checkout_session_params(lines)
    idem = "we-" + hashlib.sha256((cart_token(lines) + "|" + secrets.token_hex(8)).encode()).hexdigest()[:40]
    try:
        s = stripe_request("POST", "/checkout/sessions", params, idem=idem)
    except ShopError as e:
        log.error("checkout session failed: %s", e)
        return _json_error(502, "We could not start checkout. Please try again in a few minutes.")
    secret = s.get("client_secret") or s.get("clientSecret")
    if not secret:
        log.error("checkout session missing client secret")
        return _json_error(502, "We could not start checkout. Please try again in a few minutes.")
    return jsonify({"clientSecret": secret})


@bp.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if _rate_limited("shop-webhook", 240):
        return "", 429
    secret = env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        return "", 404
    payload = request.get_data(cache=False)
    if len(payload) > 1_000_000 or not verify_stripe_signature(payload, request.headers.get("Stripe-Signature", ""), secret):
        return jsonify({"error": "bad signature"}), 400
    try:
        event = json.loads(payload)
        etype, obj = event["type"], event["data"]["object"]
    except (ValueError, KeyError, TypeError):
        return jsonify({"error": "bad payload"}), 400
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        try:
            status, note = handle_checkout_completed(obj)
        except Exception:     # never leak details to the caller; Stripe retries on 5xx
            log.exception("webhook handler crashed")
            return jsonify({"received": False}), 500
        return jsonify({"received": status < 500, "note": note}), status
    return jsonify({"received": True, "ignored": etype}), 200


@bp.route("/shop/success")
def success():
    sid = request.args.get("session_id", "")
    info = {"paid": None, "email": None}
    if SESSION_ID_RE.match(sid) and stripe_ready():
        try:
            s = stripe_request("GET", "/checkout/sessions/" + sid)
            info["paid"] = s.get("payment_status") in ("paid", "no_payment_required")
            info["email"] = (s.get("customer_details") or {}).get("email")
        except ShopError as e:
            log.error("success lookup failed: %s", e)
    resp = make_response(render_template("shop_success.html", info=info, seller=env("SELLER_NAME", "Brunner Creative")))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@bp.route("/shop/cancel")
def cancel():
    resp = make_response(render_template("shop_cancel.html"))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def register(app, rate_limited, error_page):
    global _rate_limited, _error_page
    _rate_limited, _error_page = rate_limited, error_page
    app.register_blueprint(bp)

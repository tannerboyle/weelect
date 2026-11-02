"""Support page + small store for weelect.org.

Self-contained on weelect.org: PayPal's JavaScript SDK draws the PayPal and "Debit or Credit Card" buttons on our own
checkout page (card details are typed into PayPal's iframe, never into this server). The server creates every PayPal
order from the catalog (Orders API v2, itemized: item_total + shipping + tax_total), captures it, and only after PayPal
reports a COMPLETED capture does it create the Printful order. A verified PayPal webhook is the backstop for anything
the browser did not finish. Everything degrades to a "coming soon" panel when the PayPal variables are unset.
See docs/SHOP_SETUP.md.

Env vars (all optional; nothing is required for the rest of the site to work):
  PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET   REST app credentials (sandbox first)
  PAYPAL_WEBHOOK_ID                        id of the webhook pointing at /paypal/webhook (used to verify events)
  PAYPAL_ENV=sandbox|live                  which PayPal environment to use (default sandbox)
  PRINTFUL_API_TOKEN, PRINTFUL_STORE_ID    Printful (store id only for account-level tokens)
  PRINTFUL_AUTO_CONFIRM=1     submit Printful orders for fulfillment automatically (default: leave as drafts, confirm=false)
  SHOP_LIVE_CATALOG=0         serve only the committed snapshot data/shop_products.json (default: live Printful store, cached)
  SHOP_CATALOG_TTL, SHOP_CATALOG_RETRY, SHOP_CATALOG_CACHE   catalog cache tuning (see printful_store.py)
  SHOP_TAX_RATES              JSON of state -> percent, e.g. {"WA": 10.25, "TX": {"rate": 8.25, "shipping": true}}.
                              Tax is collected only for orders shipped to the listed states. Default: none.
  SHOP_SECRET                 signing key for CSRF tokens (defaults to a hash of PAYPAL_CLIENT_SECRET)
  SHOP_ORDER_LOG              path of the JSONL order log (default /tmp/weelect_shop_orders.jsonl)
  SHOP_CSP_REPORT_ONLY=1      send the shop pages' CSP as report-only (debugging)
  SELLER_NAME                 seller of record shown on the shop, checkout and thank-you pages and to PayPal (default "Brunner Creative LLC")
"""
import hashlib, hmac, json, os, re, secrets, threading, time, logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import requests
import printful_store
from flask import Blueprint, request, jsonify, render_template, redirect, make_response

ROOT = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("weelect.shop")

PRINTFUL_API = "https://api.printful.com"
PAYPAL_API = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}
SOURCE = "weelect-shop"            # custom_id on every PayPal shop order we create; anything else is ignored
DONATION_SOURCE = "weelect-gift"   # custom_id on one-time gifts (no Printful, no shipping, no tax)
OUR_SOURCES = (SOURCE, DONATION_SOURCE)
DONATION_MIN_CENTS, DONATION_MAX_CENTS = 100, 1_000_000
DONATION_SUGGESTIONS = (5, 10, 25, 50, 100)
DONATION_RE = re.compile(r"^\$?\s*(\d{1,3}(,\d{3})+|\d{1,6})(\.\d{1,2})?$")
CSRF_TTL = 4 * 3600
HTTP_TIMEOUT = 15
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{10,30}$")     # PayPal order ids are 17 upper-case alphanumerics today
KOFI_PAGE = "https://ko-fi.com/weelect"
KOFI_EMBED = "https://ko-fi.com/weelect/?hidefeed=true&widget=true&embed=true&preview=true"
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}
ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

bp = Blueprint("shop", __name__)
_rate_limited = lambda bucket, limit, window=60: False   # replaced by register()
_error_page = None
_LOCK = threading.Lock()


class ShopError(Exception):
    """Raised for failures we log in detail but never show to visitors."""


# ------------------------------------------------------------------ config (read at call time so tests can patch env)
def env(name, default=""):
    return (os.environ.get(name) or default).strip()


DEFAULT_SELLER = "Brunner Creative LLC"


def seller_name():
    return env("SELLER_NAME", DEFAULT_SELLER)


def site_url():
    return env("SITE_URL", "https://weelect.org").rstrip("/")


def paypal_env():
    return "live" if env("PAYPAL_ENV").lower() == "live" else "sandbox"


def paypal_api():
    return PAYPAL_API[paypal_env()]


def paypal_ready():
    return bool(env("PAYPAL_CLIENT_ID")) and bool(env("PAYPAL_CLIENT_SECRET"))


def webhook_ready():
    return bool(env("PAYPAL_WEBHOOK_ID"))


def printful_ready():
    return bool(env("PRINTFUL_API_TOKEN"))


def donations_open():
    """On-site PayPal gifts need only the PayPal credentials and webhook id (no catalog). Until then the page links to Ko-fi."""
    return paypal_ready() and webhook_ready()


def parse_donation(raw):
    """'25', '25.5', '$1,000.00' -> cents. Returns (cents, error). Whole dollars or dollars and cents; $1 to $10,000."""
    if isinstance(raw, bool) or raw is None:
        return None, "Please enter an amount."
    v = str(raw).strip() if isinstance(raw, (str, int)) else None
    if v is None or len(v) > 16 or not DONATION_RE.match(v):
        return None, "Please enter an amount in dollars, like 25 or 25.50."
    c = _cents(v.replace("$", "").replace(",", "").strip())
    if c is None or c < DONATION_MIN_CENTS:
        return None, "The smallest gift we can accept online is $1."
    if c > DONATION_MAX_CENTS:
        return None, "Online gifts are limited to $10,000. To give more, please contact us from the About page."
    return c, None


def build_donation_order(c):
    return {"intent": "CAPTURE", "purchase_units": [{
        "reference_id": "default", "custom_id": DONATION_SOURCE,
        "description": f"Gift to We Elect (weelect.org), received by {seller_name()}. Not tax-deductible."[:127],
        "amount": dict(_amt(c), breakdown={"item_total": _amt(c)}),
        "items": [{"name": "Gift to We Elect", "sku": "gift", "quantity": "1", "unit_amount": _amt(c), "category": "DONATION"}]}],
        "application_context": {"brand_name": f"We Elect ({seller_name()})"[:127], "shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}}


def shop_open():
    """The store takes orders only when PayPal credentials AND the webhook id are set; otherwise 'coming soon'."""
    return paypal_ready() and webhook_ready() and bool(catalog_products())


# ------------------------------------------------------------------ sales tax (PayPal does not calculate it)
def tax_rates():
    """SHOP_TAX_RATES -> {state: (Decimal percent, taxes_shipping)}. Bad JSON or bad entries are logged and skipped,
    so a typo never charges the wrong tax: a state that is not listed (correctly) is charged no tax."""
    raw = env("SHOP_TAX_RATES")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.error("SHOP_TAX_RATES is not valid JSON; collecting no tax")
        return {}
    out = {}
    if not isinstance(data, dict):
        log.error("SHOP_TAX_RATES must be a JSON object; collecting no tax")
        return {}
    for st, v in data.items():
        st = str(st).upper()
        ship = False
        if isinstance(v, dict):
            ship, v = bool(v.get("shipping")), v.get("rate")
        try:
            rate = Decimal(str(v))
        except (InvalidOperation, ValueError):
            rate = None
        if st not in US_STATES or rate is None or isinstance(v, bool) or not (0 <= rate <= 20):
            log.error("SHOP_TAX_RATES entry for %s ignored (state must be a US code, rate a percent 0-20)", st)
            continue
        out[st] = (rate, ship)
    return out


def tax_mode():
    return "state_rates" if tax_rates() else "none"


def tax_cents(items_cents, ship_cents, state):
    rate, ship = tax_rates().get(state, (Decimal(0), False))
    base = items_cents + (ship_cents if ship else 0)
    return int((Decimal(base) * rate / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------ catalog
def catalog():
    """Products live in the Printful store; printful_store caches them (memory, disk, committed snapshot)."""
    return printful_store.get()


def variant_ok(p, color_id, size):
    v = (p.get("variants") or {}).get(f"{color_id}:{size}")
    return bool(v and (v.get("sync_variant_id") or v.get("printful_variant_id")) and variant_price(p, v) > 0)


def variant_price(p, v):
    """Retail price as set in Printful for this exact variant (sizes can differ), falling back to the product price."""
    return int((v or {}).get("price_cents") or p.get("price_cents") or 0)


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
        v = p["variants"][f"{cid}:{size}"]
        lines.append({"p": pid, "c": cid, "s": size, "q": merged[(pid, cid, size)], "name": label, "price_cents": variant_price(p, v),
                      "variant_id": v.get("printful_variant_id"), "sync_variant_id": v.get("sync_variant_id")})
    return lines, None


def subtotal_cents(lines):
    return sum(l["price_cents"] * l["q"] for l in lines)


SHIP_LABEL = "Standard shipping (U.S.)"
SHIP_CACHE_TTL = 600
_SHIP = {}


class ShippingUnavailable(Exception):
    """Printful could not give a shipping rate; checkout asks the buyer to try again rather than guess."""


def shipping_cents(lines, addr):
    """Printful's live STANDARD rate for these items to this address (POST /shipping/rates, an estimate only; it creates
    nothing). Cached for 10 minutes per cart + destination, so the quote and the PayPal order agree."""
    items = sorted((int(l["variant_id"]), int(l["q"])) for l in lines if l.get("variant_id"))
    if len(items) != len(lines) or not printful_ready():
        raise ShippingUnavailable("no catalog variant ids or no Printful token")
    key = (addr.get("state"), (addr.get("zip") or "")[:5], tuple(items))
    now = time.time()
    hit = _SHIP.get(key)
    if hit and now - hit[1] < SHIP_CACHE_TTL:
        return hit[0]
    recipient = {"country_code": "US", "state_code": addr.get("state"), "zip": (addr.get("zip") or "")[:5],
                 "city": addr.get("city") or "", "address1": addr.get("line1") or ""}
    rates, err = printful_shipping_rates(recipient, [{"variant_id": v, "quantity": q} for v, q in items])
    if not rates:
        log.warning("printful shipping rates unavailable: %s", err or "no rates")
        raise ShippingUnavailable(err or "no rates")
    pick = next((r for r in rates if r.get("id") == "STANDARD"), None) or min(rates, key=lambda r: _cents(r.get("rate")) or 10**9)
    c = _cents(pick.get("rate"))
    if c is None or c < 0:
        raise ShippingUnavailable("bad rate")
    if len(_SHIP) > 500:
        _SHIP.clear()
    _SHIP[key] = (c, now)
    return c


def cart_token(lines):
    """Compact cart for the order log: 'tee.navy.M:2,hat.navy.One size:1'."""
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


# ------------------------------------------------------------------ shipping address (our own form; sent to PayPal as
# SET_PROVIDED_ADDRESS so the buyer pays for exactly this address, and Printful later reads it back from PayPal's order)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(v, n):
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", _CTRL.sub(" ", v)).strip()
    return v if len(v) <= n else None


def parse_address(raw):
    """Validate {name, line1, line2, city, state, zip}. Returns (address, error). U.S. addresses only."""
    if not isinstance(raw, dict):
        return None, "Please enter a shipping address."
    a = {"name": _clean(raw.get("name"), 120), "line1": _clean(raw.get("line1"), 120), "line2": _clean(raw.get("line2") or "", 120),
         "city": _clean(raw.get("city"), 80), "state": _clean(raw.get("state"), 2), "zip": _clean(raw.get("zip"), 10)}
    if not a["name"]:
        return None, "Please enter the name to ship to."
    if not a["line1"]:
        return None, "Please enter a street address."
    if a["line2"] is None:
        return None, "The second address line is too long."
    if not a["city"]:
        return None, "Please enter a city."
    if (a["state"] or "").upper() not in US_STATES:
        return None, "Please choose a state. We ship within the United States only."
    a["state"] = a["state"].upper()
    if not ZIP_RE.match(a["zip"] or ""):
        return None, "Please enter a 5-digit ZIP code."
    return a, None


def quote(lines, addr, ship=None):
    """All money for an order, in cents, computed on the server from the catalog (Printful retail prices), Printful's
    shipping rate for this address and SHOP_TAX_RATES. Pass ship to re-check an order whose shipping is already fixed."""
    items = subtotal_cents(lines)
    if ship is None:
        ship = shipping_cents(lines, addr)
    tax = tax_cents(items, ship, addr.get("state"))
    return {"items": items, "shipping": ship, "tax": tax, "total": items + ship + tax}


def public_quote(lines, q):
    return {"lines": [{"name": l["name"], "q": l["q"], "amount": money(l["price_cents"] * l["q"])} for l in lines],
            "items": money(q["items"]), "shipping": money(q["shipping"]), "tax": money(q["tax"]), "total": money(q["total"]),
            "shipping_label": SHIP_LABEL}


# ------------------------------------------------------------------ CSRF (signed token + double-submit cookie + Origin check)
def _csrf_key():
    k = env("SHOP_SECRET") or env("PAYPAL_CLIENT_SECRET")
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
# PayPal's documented CSP for the JS SDK (developer.paypal.com/sdk/js/best-practices): *.paypal.com and *.paypalobjects.com
# for scripts, styles, images, XHR and frames. Venmo and Pay Later are switched off in the SDK URL, so *.venmo.com is not
# needed. GoatCounter (site-wide analytics) needs its script host and its count endpoint.
PAYPAL_SRC = "https://*.paypal.com https://*.paypalobjects.com"
GOAT = "https://weelect.goatcounter.com"


def shop_csp():
    return "; ".join([
        "default-src 'self'",
        # The site uses a few small inline scripts/styles, so 'unsafe-inline' stays (PayPal's own recommendation too).
        f"script-src 'self' 'unsafe-inline' {PAYPAL_SRC} https://gc.zgo.at",
        f"style-src 'self' 'unsafe-inline' {PAYPAL_SRC}",
        f"font-src 'self' {PAYPAL_SRC}",
        f"img-src 'self' data: {PAYPAL_SRC} https://files.cdn.printful.com https://ko-fi.com https://storage.ko-fi.com {GOAT}",
        f"connect-src 'self' {PAYPAL_SRC} {GOAT}",
        f"frame-src {PAYPAL_SRC} https://ko-fi.com",
        f"child-src {PAYPAL_SRC} https://ko-fi.com",
        "frame-ancestors 'none'", "base-uri 'self'", "object-src 'none'",
        # The buttons talk to our server with fetch and to PayPal inside PayPal's own frames/popup; no form posts leave the site.
        "form-action 'self'",
    ])


def shop_headers(resp, payment=True):
    name = "Content-Security-Policy-Report-Only" if env("SHOP_CSP_REPORT_ONLY") == "1" else "Content-Security-Policy"
    resp.headers[name] = shop_csp()
    if payment:   # the site-wide default is payment=(); shop pages allow PayPal's and Ko-fi's frames to use it
        resp.headers["Permissions-Policy"] = ('geolocation=(), camera=(), microphone=(), '
                                              'payment=(self "https://www.paypal.com" "https://www.sandbox.paypal.com" "https://ko-fi.com")')
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ PayPal REST (plain requests; no SDK)
_TOKEN = {"key": None, "token": None, "exp": 0}


def paypal_token(force=False):
    key = (paypal_env(), env("PAYPAL_CLIENT_ID"))
    with _LOCK:
        if not force and _TOKEN["key"] == key and _TOKEN["token"] and time.time() < _TOKEN["exp"]:
            return _TOKEN["token"]
    try:
        r = requests.post(paypal_api() + "/v1/oauth2/token", auth=(env("PAYPAL_CLIENT_ID"), env("PAYPAL_CLIENT_SECRET")),
                          data={"grant_type": "client_credentials"}, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ShopError(f"paypal token network error: {type(e).__name__}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("access_token"):
        raise ShopError(f"paypal token {r.status_code} {body.get('error') if isinstance(body, dict) else ''}")
    with _LOCK:
        _TOKEN.update(key=key, token=body["access_token"], exp=time.time() + max(60, int(body.get("expires_in", 3600)) - 120))
    return body["access_token"]


def paypal_request(method, path, body=None, request_id=None, prefer=None):
    """Returns (status, json). Raises ShopError on network failure or a credentials problem."""
    for attempt in (0, 1):
        h = {"Authorization": "Bearer " + paypal_token(force=attempt == 1), "Content-Type": "application/json", "Accept": "application/json"}
        if request_id:
            h["PayPal-Request-Id"] = request_id
        if prefer:
            h["Prefer"] = prefer
        try:
            r = requests.request(method, paypal_api() + path, headers=h, data=None if body is None else json.dumps(body), timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise ShopError(f"paypal network error: {type(e).__name__}")
        if r.status_code == 401 and attempt == 0:
            continue                    # token expired early: fetch a new one once
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}
    raise ShopError("paypal 401 after token refresh")


def _amt(cents):
    return {"currency_code": "USD", "value": f"{cents/100:.2f}"}


def _cents(v):
    try:
        return int((Decimal(str(v)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return None


def build_paypal_order(lines, addr):
    q = quote(lines, addr)
    pu = {
        "reference_id": "default",
        "custom_id": SOURCE,
        "description": "We Elect Gifts order",
        "amount": dict(_amt(q["total"]), breakdown={"item_total": _amt(q["items"]), "shipping": _amt(q["shipping"]),
                                                    "tax_total": _amt(q["tax"])}),
        "items": [{"name": l["name"][:127], "sku": f"{l['p']}.{l['c']}.{l['s']}", "quantity": str(l["q"]),
                   "unit_amount": _amt(l["price_cents"]), "category": "PHYSICAL_GOODS"} for l in lines],
        "shipping": {"name": {"full_name": addr["name"]},
                     "address": {"address_line_1": addr["line1"], "address_line_2": addr["line2"] or None, "admin_area_2": addr["city"],
                                 "admin_area_1": addr["state"], "postal_code": addr["zip"], "country_code": "US"}},
    }
    pu["shipping"]["address"] = {k: v for k, v in pu["shipping"]["address"].items() if v}
    return {"intent": "CAPTURE", "purchase_units": [pu],
            # SET_PROVIDED_ADDRESS: PayPal shows (and charges for) exactly the address we computed tax and shipping for.
            "application_context": {"brand_name": f"We Elect ({seller_name()})"[:127], "shipping_preference": "SET_PROVIDED_ADDRESS", "user_action": "PAY_NOW"}}, q


def unit_of(order):
    pus = order.get("purchase_units") or []
    return pus[0] if pus and isinstance(pus[0], dict) else {}


def capture_of(order):
    caps = ((unit_of(order).get("payments") or {}).get("captures")) or []
    return caps[0] if caps and isinstance(caps[0], dict) else {}


def lines_from_order(order):
    """Re-read the cart from the PayPal order's items (sku = product.color.size) and re-validate it against the catalog."""
    raw = []
    for it in unit_of(order).get("items") or []:
        try:
            pid, cid, size = str(it["sku"]).split(".", 2)
            raw.append({"p": pid, "c": cid, "s": size, "q": int(it["quantity"])})
        except (KeyError, ValueError, TypeError):
            return None, "unreadable item"
    return parse_cart(raw)


def order_address(order):
    sh = unit_of(order).get("shipping") or {}
    a = sh.get("address") or {}
    return {"name": (sh.get("name") or {}).get("full_name"), "line1": a.get("address_line_1"), "line2": a.get("address_line_2") or "",
            "city": a.get("admin_area_2"), "state": a.get("admin_area_1"), "zip": a.get("postal_code"), "country": a.get("country_code")}


def get_order(order_id):
    status, body = paypal_request("GET", "/v2/checkout/orders/" + order_id)
    if status != 200:
        raise ShopError(f"paypal get order {status} {body.get('name')}")
    return body


def capture_order(order_id):
    """Capture an approved order. Safe to repeat: PayPal-Request-Id makes the call idempotent, and an already-captured
    order is simply read back. Returns (outcome, order) with outcome one of completed, pending, declined, failed."""
    status, body = paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture", body={},
                                  request_id="we-capture-" + order_id, prefer="return=representation")
    issues = {d.get("issue") for d in (body.get("details") or []) if isinstance(d, dict)}
    if status == 422 and "ORDER_ALREADY_CAPTURED" in issues:
        body, status = get_order(order_id), 200
    elif status == 422 and "INSTRUMENT_DECLINED" in issues:
        return "declined", body
    if status not in (200, 201):
        log.error("paypal capture %s failed: %s %s %s", order_id, status, body.get("name"), sorted(i for i in issues if i))
        return "failed", body
    if not unit_of(body).get("items"):
        body = get_order(order_id)         # the capture response may omit items; the order read always has them
    cap = capture_of(body).get("status")
    if cap == "COMPLETED":
        return "completed", body
    if cap == "PENDING":
        return "pending", body
    return ("declined" if cap in ("DECLINED", "FAILED") else "failed"), body


def verify_webhook(headers, event):
    """Ask PayPal whether this event really came from PayPal for our webhook (POST /v1/notifications/verify-webhook-signature)."""
    names = {"auth_algo": "PAYPAL-AUTH-ALGO", "cert_url": "PAYPAL-CERT-URL", "transmission_id": "PAYPAL-TRANSMISSION-ID",
             "transmission_sig": "PAYPAL-TRANSMISSION-SIG", "transmission_time": "PAYPAL-TRANSMISSION-TIME"}
    body = {k: headers.get(h, "") for k, h in names.items()}
    if not all(body.values()) or not webhook_ready():
        return False
    body["webhook_id"] = env("PAYPAL_WEBHOOK_ID")
    body["webhook_event"] = event
    try:
        status, res = paypal_request("POST", "/v1/notifications/verify-webhook-signature", body=body)
    except ShopError as e:
        log.error("webhook verification call failed: %s", e)
        return None          # could not ask PayPal: make PayPal retry later rather than drop the event
    return status == 200 and res.get("verification_status") == "SUCCESS"


# ------------------------------------------------------------------ Printful
def printful_headers():
    h = {"Authorization": "Bearer " + env("PRINTFUL_API_TOKEN"), "Content-Type": "application/json"}
    if env("PRINTFUL_STORE_ID"):
        h["X-PF-Store-Id"] = env("PRINTFUL_STORE_ID")
    return h


def external_id(order_id):
    """Printful external ids are unique per store (<=32 chars), which makes order creation idempotent across workers and
    restarts. One PayPal order = one Printful order: 'pp' + the PayPal order id."""
    return "pp" + order_id


def build_printful_order(order, lines):
    a = order_address(order)
    recipient = {"name": a["name"] or "Customer", "address1": a["line1"], "address2": a["line2"], "city": a["city"],
                 "state_code": a["state"], "country_code": a["country"] or "US", "zip": a["zip"]}
    email = (order.get("payer") or {}).get("email_address")
    if email:
        recipient["email"] = email    # Printful sends the shipping/tracking notice; We Elect sends no email itself
    items = []
    for l in lines:
        # sync_variant_id = the product as designed in the Printful store, so Printful uses Bob's own print files and placements.
        it = {"sync_variant_id": l["sync_variant_id"]} if l.get("sync_variant_id") else {"variant_id": l["variant_id"]}
        it.update({"quantity": l["q"], "name": l["name"], "retail_price": f"{l['price_cents']/100:.2f}"})
        items.append(it)
    bd = (unit_of(order).get("amount") or {}).get("breakdown") or {}
    return {"external_id": external_id(order["id"]), "shipping": "STANDARD", "recipient": recipient, "items": items,
            "retail_costs": {"currency": "USD", "subtotal": f"{subtotal_cents(lines)/100:.2f}",
                             "shipping": (bd.get("shipping") or {}).get("value", "0.00"),
                             "tax": (bd.get("tax_total") or {}).get("value", "0.00")}}


def printful_order_exists(ext_id):
    """True / False, or None when Printful could not be asked (then creation still runs; Printful rejects duplicates)."""
    try:
        r = requests.get(PRINTFUL_API + "/orders/@" + ext_id, headers=printful_headers(), timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    return None


def printful_create_order(payload, confirm):
    """confirm=False (the default unless PRINTFUL_AUTO_CONFIRM=1) leaves the order as a Printful draft: nothing is
    printed or charged until it is confirmed in the Printful dashboard."""
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
    """Printful shipping rates for a recipient + [{variant_id, quantity}]. Returns (rates, error). Read-only estimate."""
    try:
        r = requests.post(PRINTFUL_API + "/shipping/rates", headers=printful_headers(),
                          data=json.dumps({"recipient": recipient, "items": items, "currency": "USD"}), timeout=HTTP_TIMEOUT)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, f"printful {r.status_code}"
    return body.get("result") or [], None


# ------------------------------------------------------------------ order log + fulfilment (shared by capture and webhook)
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
                out[rec.get("order_id")] = rec
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


def fulfil(order):
    """Create the Printful order for a PayPal order, but only if PayPal says its capture is COMPLETED.
    Returns (http_status, note). 5xx asks PayPal (webhook) to retry later; everything else is final."""
    oid = str(order.get("id") or "")
    if not ORDER_ID_RE.match(oid):
        return 200, "ignored: bad order id"
    if unit_of(order).get("custom_id") != SOURCE:
        return 200, "ignored: not a shop order"
    cap = capture_of(order)
    if cap.get("status") != "COMPLETED":
        return 200, "ignored: capture not completed"
    with _LOCK:
        prev = read_log().get(oid)
        if prev and prev.get("state") in ("created", "failed_permanent"):
            return 200, "duplicate"
        lines, err = lines_from_order(order)
        if err:
            write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "failed_permanent", "error": "cart: " + str(err)})
            return 200, "needs manual attention"
        addr = order_address(order)
        # Shipping was fixed (from Printful's rate) when the PayPal order was created; re-check items and tax against it.
        ship = _cents((((unit_of(order).get("amount") or {}).get("breakdown") or {}).get("shipping") or {}).get("value"))
        q = quote(lines, addr, ship=ship if ship is not None and ship >= 0 else 0)
        paid = _cents((cap.get("amount") or {}).get("value"))
        amounts_ok = paid == q["total"] and (cap.get("amount") or {}).get("currency_code") == "USD"
        if not printful_ready():
            write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "failed_permanent",
                       "error": "PRINTFUL_API_TOKEN not set; create order by hand", "cart": cart_token(lines)})
            return 200, "needs manual attention"
        ext = external_id(oid)
        if printful_order_exists(ext):
            write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "created", "printful_status": "duplicate", "cart": cart_token(lines)})
            return 200, "duplicate"
        confirm = env("PRINTFUL_AUTO_CONFIRM") == "1" and amounts_ok
        res = printful_create_order(build_printful_order(order, lines), confirm)
        if res["ok"]:
            write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "created", "printful_id": res.get("printful_id"),
                       "printful_status": res.get("status"), "confirmed": bool(confirm), "amounts_match": amounts_ok,
                       "cart": cart_token(lines), "paid_cents": paid, "tax_cents": q["tax"], "ship_state": addr.get("state")})
            return 200, "created"
        if res.get("retry"):
            write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "retrying", "error": res["error"]})
            return 500, "retry"
        write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "failed_permanent", "error": res["error"], "cart": cart_token(lines)})
        return 200, "needs manual attention"


# ------------------------------------------------------------------ routes
SHIP_DOWN = "We could not get a shipping rate from our printer just now. Please try again in a few minutes."


def _json_error(status, msg):
    return jsonify({"error": msg}), status


def _page_context():
    products = catalog_products()
    pub = []
    for p in products:
        ok = {f"{c['id']}:{s}": variant_ok(p, c["id"], s) for c in p["colors"] for s in p["sizes"]}
        prices = {k: variant_price(p, p["variants"][k]) for k, good in ok.items() if good}
        lo, hi = (min(prices.values()), max(prices.values())) if prices else (p.get("price_cents", 0),) * 2
        pub.append({"id": p["id"], "name": p["name"], "kind": p.get("kind", "item"), "blurb": p.get("blurb", ""),
                    "details": p.get("details", []), "tier": p.get("tier") or {}, "image": p.get("image"),
                    "price_cents": lo, "price": money(lo) if lo == hi else f"{money(lo)}\u2013{money(hi)}",
                    "price_from": money(lo) if lo == hi else "From " + money(lo), "colors": p["colors"],
                    "sizes": [s for s in p["sizes"] if any(ok.get(f"{c['id']}:{s}") for c in p["colors"])],
                    "ok": ok, "prices": prices})
    kinds = [p["kind"] if p.get("kind") not in (None, "item") else p["name"] for p in pub]
    phrase = kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1] if kinds else "shop items"
    return {"products": pub, "products_phrase": phrase, "shop_open": shop_open(), "donations_open": donations_open(),
            "donation_suggestions": DONATION_SUGGESTIONS, "kofi_page": KOFI_PAGE, "kofi_embed": KOFI_EMBED,
            "seller": seller_name(), "tax_mode": tax_mode(), "tax_states": sorted(tax_rates()), "shipping_label": SHIP_LABEL,
            "shop_json": {"products": [{k: p[k] for k in ("id", "name", "price_cents", "colors", "ok", "prices")} for p in pub],
                          "limits": catalog().get("limits", {})}}


def _guard(bucket, limit, is_open=None):
    """Common checks for the browser-facing JSON endpoints. Returns an error response or None."""
    if _rate_limited(bucket, limit):
        return _json_error(429, "Too many requests. Please wait a minute and try again.")
    if not (is_open or shop_open)():
        return _json_error(503, "The store is not open yet.")
    if not origin_ok() or not check_csrf(request.headers.get("X-CSRF-Token", ""), request.cookies.get("we_csrf", "")):
        return _json_error(403, "Your session expired. Please reload the page and try again.")
    if request.content_length is not None and request.content_length > 4096:
        return _json_error(413, "That request was too large.")
    return None


def _cart_and_address():
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    lines, err = parse_cart(data.get("items"))
    if err:
        return None, None, err
    addr, err = parse_address(data.get("address"))
    if err:
        return None, None, err
    return lines, addr, None


@bp.route("/support")
def support():
    ctx = _page_context()
    token = make_csrf() if (ctx["shop_open"] or ctx["donations_open"]) else None
    resp = make_response(render_template("support.html", csrf=token, paypal_client_id=env("PAYPAL_CLIENT_ID") if ctx["donations_open"] else "", **ctx))
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
    ctx = _page_context()
    resp = make_response(render_template("shop_checkout.html", csrf=token, paypal_client_id=env("PAYPAL_CLIENT_ID"),
                                         states=sorted(US_STATES.items(), key=lambda kv: kv[1]), seller=ctx["seller"],
                                         tax_states=ctx["tax_states"]))
    set_csrf_cookie(resp, token)
    return shop_headers(resp)


@bp.route("/shop/quote", methods=["POST"])
def quote_route():
    bad = _guard("shop-quote", 30)
    if bad:
        return bad
    lines, addr, err = _cart_and_address()
    if err:
        return _json_error(400, err)
    try:
        return jsonify(public_quote(lines, quote(lines, addr)))
    except ShippingUnavailable:
        return _json_error(503, SHIP_DOWN)


@bp.route("/shop/paypal/order", methods=["POST"])
def create_order():
    bad = _guard("shop-checkout", 12)
    if bad:
        return bad
    lines, addr, err = _cart_and_address()
    if err:
        return _json_error(400, err)
    try:
        body, q = build_paypal_order(lines, addr)
    except ShippingUnavailable:
        return _json_error(503, SHIP_DOWN)
    try:
        status, res = paypal_request("POST", "/v2/checkout/orders", body=body, request_id="we-create-" + secrets.token_hex(12))
    except ShopError as e:
        log.error("create order failed: %s", e)
        return _json_error(502, "We could not start checkout. Please try again in a few minutes.")
    if status not in (200, 201) or not ORDER_ID_RE.match(str(res.get("id") or "")):
        log.error("create order failed: %s %s %s", status, res.get("name"), [d.get("issue") for d in res.get("details") or [] if isinstance(d, dict)])
        return _json_error(502, "We could not start checkout. Please try again in a few minutes.")
    return jsonify({"id": res["id"], "total": money(q["total"])})


@bp.route("/shop/paypal/donation", methods=["POST"])
def create_donation():
    bad = _guard("shop-donate", 12, donations_open)
    if bad:
        return bad
    data = request.get_json(silent=True)
    c, err = parse_donation((data if isinstance(data, dict) else {}).get("amount"))
    if err:
        return _json_error(400, err)
    try:
        status, res = paypal_request("POST", "/v2/checkout/orders", body=build_donation_order(c), request_id="we-gift-" + secrets.token_hex(12))
    except ShopError as e:
        log.error("create donation failed: %s", e)
        return _json_error(502, "We could not start your gift. Please try again in a few minutes.")
    if status not in (200, 201) or not ORDER_ID_RE.match(str(res.get("id") or "")):
        log.error("create donation failed: %s %s", status, res.get("name"))
        return _json_error(502, "We could not start your gift. Please try again in a few minutes.")
    return jsonify({"id": res["id"], "total": money(c)})


@bp.route("/shop/paypal/order/<oid>/capture", methods=["POST"])
def capture_route(oid):
    bad = _guard("shop-capture", 12, lambda: shop_open() or donations_open())
    if bad:
        return bad
    if not ORDER_ID_RE.match(oid):
        return _json_error(400, "That order could not be found.")
    try:
        outcome, order = capture_order(oid)
    except ShopError as e:
        log.error("capture %s failed: %s", oid, e)
        return _json_error(502, "We could not confirm your payment. You have not been charged twice; please try again in a minute.")
    if outcome == "declined":
        return jsonify({"restart": True, "error": "Your payment was declined. Please try another card or payment method."}), 402
    if outcome == "failed":
        return _json_error(502, "We could not complete your payment. You have not been charged. Please try again.")
    if unit_of(order).get("custom_id") not in OUR_SOURCES:
        return _json_error(400, "That order could not be found.")
    if unit_of(order).get("custom_id") == DONATION_SOURCE:
        cap = capture_of(order)
        write_log({"order_id": oid, "capture_id": cap.get("id"), "state": "gift_" + outcome, "kind": "gift",
                   "paid_cents": _cents((cap.get("amount") or {}).get("value"))})
        return jsonify({"status": outcome, "redirect": "/shop/success?order=" + oid})
    if outcome == "pending":
        write_log({"order_id": oid, "capture_id": capture_of(order).get("id"), "state": "pending",
                   "reason": (capture_of(order).get("status_details") or {}).get("reason")})
        return jsonify({"status": "pending", "redirect": "/shop/success?order=" + oid})
    try:
        fulfil(order)
    except Exception:            # the buyer has paid; never fail their page because of fulfilment, the webhook retries
        log.exception("fulfil after capture crashed for %s", oid)
    return jsonify({"status": "completed", "redirect": "/shop/success?order=" + oid})


@bp.route("/paypal/webhook", methods=["POST"])
def paypal_webhook():
    if _rate_limited("shop-webhook", 240):
        return "", 429
    if not (paypal_ready() and webhook_ready()):
        return "", 404
    payload = request.get_data(cache=False)
    if len(payload) > 1_000_000:
        return jsonify({"error": "too large"}), 413
    try:
        event = json.loads(payload)
        etype, res = event["event_type"], event["resource"]
        if not isinstance(res, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return jsonify({"error": "bad payload"}), 400
    ok = verify_webhook(request.headers, event)
    if ok is None:
        return jsonify({"error": "could not verify"}), 503
    if not ok:
        log.warning("paypal webhook signature failed for %s", etype)
        return jsonify({"error": "bad signature"}), 400
    try:
        if etype == "PAYMENT.CAPTURE.COMPLETED":
            oid = str(((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id") or "")
            if not ORDER_ID_RE.match(oid):
                return jsonify({"received": True, "note": "ignored: no order id"}), 200
            status, note = fulfil(get_order(oid))
        elif etype in ("CHECKOUT.ORDER.APPROVED", "CHECKOUT.ORDER.COMPLETED"):
            oid = str(res.get("id") or "")
            if not ORDER_ID_RE.match(oid) or unit_of(res).get("custom_id") not in OUR_SOURCES:
                return jsonify({"received": True, "note": "ignored"}), 200
            if etype == "CHECKOUT.ORDER.APPROVED":
                # The buyer approved but the browser never asked us to capture (closed tab, lost connection): capture now.
                # Same PayPal-Request-Id as the browser path, so this can never charge twice.
                outcome, order = capture_order(oid)
                if outcome == "failed":
                    return jsonify({"received": False}), 500
            else:
                order = get_order(oid)
            status, note = fulfil(order)
        elif etype in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.DENIED"):
            write_log({"order_id": str(((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id") or ""),
                       "capture_id": res.get("id"), "state": "notice", "event": etype})
            return jsonify({"received": True, "note": "logged"}), 200
        else:
            return jsonify({"received": True, "ignored": etype}), 200
    except ShopError as e:
        log.error("webhook %s: %s", etype, e)
        return jsonify({"received": False}), 500
    except Exception:     # never leak details to the caller; PayPal retries on non-2xx
        log.exception("webhook handler crashed")
        return jsonify({"received": False}), 500
    return jsonify({"received": status < 500, "note": note}), status


@bp.route("/shop/success")
def success():
    oid = request.args.get("order", "")
    info = {"paid": None, "pending": False, "email": None, "gift": False, "amount": None, "receipt": None, "date": None}
    if ORDER_ID_RE.match(oid) and paypal_ready():
        try:
            order = get_order(oid)
            if unit_of(order).get("custom_id") in OUR_SOURCES:
                cap = capture_of(order)
                st = cap.get("status")
                info["paid"] = st == "COMPLETED"
                info["pending"] = st == "PENDING"
                info["email"] = (order.get("payer") or {}).get("email_address")
                info["gift"] = unit_of(order).get("custom_id") == DONATION_SOURCE
                c = _cents((cap.get("amount") or {}).get("value"))
                info["amount"] = money(c) if c is not None else None
                info["receipt"] = cap.get("id")
                info["date"] = (cap.get("create_time") or "")[:10] or None
        except ShopError as e:
            log.error("success lookup failed: %s", e)
    resp = make_response(render_template("shop_success.html", info=info, seller=seller_name(), order_id=oid if info["receipt"] else None))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def register(app, rate_limited, error_page):
    global _rate_limited, _error_page
    _rate_limited, _error_page = rate_limited, error_page
    app.register_blueprint(bp)

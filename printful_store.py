"""Live shop catalog from the Printful store (sync products), with caching and fallbacks.

Where the shop's products come from, in order:
  1. memory: the last good catalog built from Printful, kept for SHOP_CATALOG_TTL seconds (default 900);
  2. disk:   the same catalog saved to SHOP_CATALOG_CACHE (default /tmp/weelect_printful_catalog.json), so a restart
             serves the last good copy at once;
  3. data/shop_products.json: a snapshot of the Printful store committed to the repo (scripts/printful_sync.py --snapshot),
             used when Printful has never answered on this server (or is down) and there is no disk copy.
A stale catalog is refreshed in a background thread, so a page view never waits on Printful. If Printful fails, the last
good catalog keeps being served and the next attempt waits SHOP_CATALOG_RETRY seconds (default 300).

Only read-only Printful calls are made here: GET /store/products, GET /store/products/{id} and the public catalog
GET /products/{catalog_product_id} (for swatch colors and the blank's model name).

Names, sizes, colors and retail prices come from Printful exactly as set in the Printful dashboard. The short copy
(blurb, bullet points, tier text, the short id used in the cart) comes from data/shop_copy.json, keyed by the Printful
product id, with sensible fallbacks for a product added in Printful later.
"""
import hashlib, json, logging, os, re, threading, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(ROOT, "data", "shop_products.json")
COPY_PATH = os.path.join(ROOT, "data", "shop_copy.json")
LOCAL_IMG_DIR = os.path.join(ROOT, "static", "shop", "pf")
PRINTFUL_API = "https://api.printful.com"
TIMEOUT = 10
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "One size"]
log = logging.getLogger("weelect.shop.catalog")


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def ttl():
    try:
        return max(60, int(env("SHOP_CATALOG_TTL", "900")))
    except ValueError:
        return 900


def retry_after():
    try:
        return max(30, int(env("SHOP_CATALOG_RETRY", "300")))
    except ValueError:
        return 300


def live_enabled():
    """SHOP_LIVE_CATALOG=0 serves only the committed snapshot (tests, or if Printful ever misbehaves)."""
    return env("SHOP_LIVE_CATALOG", "1") != "0" and bool(env("PRINTFUL_API_TOKEN"))


def cache_path():
    return env("SHOP_CATALOG_CACHE", "/tmp/weelect_printful_catalog.json")


def headers():
    h = {"Authorization": "Bearer " + env("PRINTFUL_API_TOKEN")}
    if env("PRINTFUL_STORE_ID"):
        h["X-PF-Store-Id"] = env("PRINTFUL_STORE_ID")
    return h


# ------------------------------------------------------------------ building the catalog from Printful's answers
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "default"


def cents(v):
    try:
        return int((Decimal(str(v)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return None


def guess_kind(name):
    n = (name or "").lower()
    for kind, words in (("tee", ("t-shirt", "tee", "shirt")), ("cap", ("cap", "hat", "beanie")), ("tote", ("tote", "bag")),
                        ("mug", ("mug",)), ("sticker", ("sticker",))):
        if any(w in n for w in words):
            return kind
    return "item"


def load_copy():
    try:
        with open(COPY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def local_image(url):
    """Mockups mirrored into static/shop/pf/ by scripts/printful_sync.py --snapshot are served from our own site;
    anything newer is shown from Printful's CDN until the next snapshot."""
    if not url:
        return None
    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
    if os.path.exists(os.path.join(LOCAL_IMG_DIR, name)):
        return "/static/shop/pf/" + name
    return url


def _preview(v):
    for f in v.get("files") or []:
        if f.get("type") == "preview" and f.get("preview_url"):
            return f["preview_url"]
    return None


def build_product(detail, catalog_info, copy):
    """detail = GET /store/products/{id} result; catalog_info = {catalog_variant_id: {color_code, model}}."""
    sp, svs = detail["sync_product"], detail.get("sync_variants") or []
    pid_num = str(sp["id"])
    c = copy.get(pid_num) or {}
    kind = c.get("kind") or guess_kind(sp.get("name"))
    colors, sizes, variants, seen_sizes = [], [], {}, []
    by_color = {}
    for v in svs:
        if v.get("is_ignored") or v.get("availability_status") not in (None, "active"):
            continue
        price = cents(v.get("retail_price"))
        if not price or price <= 0 or (v.get("currency") or "USD").upper() != "USD":
            continue
        cat_vid = v.get("variant_id") or (v.get("product") or {}).get("variant_id")
        color_name = v.get("color") or "Default"
        size = v.get("size") or "One size"
        cid = slug(color_name)
        if cid not in by_color:
            info = catalog_info.get(cat_vid) or {}
            by_color[cid] = {"id": cid, "name": color_name, "swatch": info.get("color_code") or "#cccccc",
                             "image": local_image(_preview(v) or sp.get("thumbnail_url"))}
            colors.append(by_color[cid])
        if size not in seen_sizes:
            seen_sizes.append(size)
        variants[f"{cid}:{size}"] = {"printful_variant_id": cat_vid, "sync_variant_id": v["id"], "price_cents": price}
    sizes = sorted(seen_sizes, key=lambda s: (SIZE_ORDER.index(s) if s in SIZE_ORDER else 99, seen_sizes.index(s)))
    prices = [v["price_cents"] for v in variants.values()]
    model = next((catalog_info.get(v.get("variant_id"), {}).get("model") for v in svs if catalog_info.get(v.get("variant_id"))), None)
    details = list(c.get("details") or ([model, "Made to order and shipped by Printful"] if model else ["Made to order and shipped by Printful"]))
    return {
        "id": c.get("id") or (kind if kind != "item" else "p" + pid_num),
        "printful_product_id": sp["id"],
        "available": bool(variants) and not sp.get("is_ignored"),
        "name": sp.get("name") or "We Elect item",
        "kind": kind,
        "blurb": c.get("blurb") or "",
        "details": details,
        "tier": c.get("tier") or {},
        "order": c.get("order", 50),
        "price_cents": min(prices) if prices else 0,
        "price_max_cents": max(prices) if prices else 0,
        "image": local_image(sp.get("thumbnail_url")),
        "colors": colors,
        "sizes": sizes,
        "variants": variants,
    }


def fetch_catalog_info(catalog_product_id, session=requests):
    """Public Printful catalog (no token): swatch color codes and the blank's model, per catalog variant id."""
    r = session.get(f"{PRINTFUL_API}/products/{catalog_product_id}", timeout=TIMEOUT)
    if r.status_code != 200:
        return {}
    res = (r.json() or {}).get("result") or {}
    prod = res.get("product") or {}
    model = prod.get("title")
    return {v["id"]: {"color_code": v.get("color_code"), "model": model} for v in res.get("variants") or [] if "id" in v}


def fetch_from_printful(session=requests):
    """Build the whole catalog from the Printful store. Raises on any failure (callers fall back)."""
    h = headers()
    products, offset = [], 0
    while True:
        r = session.get(f"{PRINTFUL_API}/store/products?limit=100&offset={offset}", headers=h, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"printful store/products {r.status_code}")
        body = r.json() or {}
        page = body.get("result") or []
        products += page
        total = ((body.get("paging") or {}).get("total")) or len(products)
        offset += len(page)
        if not page or offset >= total:
            break
    copy = load_copy()
    out, cat_cache = [], {}
    for p in products:
        if p.get("is_ignored"):
            continue
        r = session.get(f"{PRINTFUL_API}/store/products/{p['id']}", headers=h, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"printful store/products/{p['id']} {r.status_code}")
        detail = (r.json() or {}).get("result") or {}
        info = {}
        for v in detail.get("sync_variants") or []:
            cp = (v.get("product") or {}).get("product_id")
            if cp and cp not in cat_cache:
                try:
                    cat_cache[cp] = fetch_catalog_info(cp, session)
                except (requests.RequestException, ValueError):
                    cat_cache[cp] = {}
            info.update(cat_cache.get(cp) or {})
        out.append(build_product(detail, info, copy))
    out = [p for p in out if p["available"]]
    if not out:
        raise RuntimeError("printful store has no sellable products")
    ids = set()
    for p in out:                      # keep cart ids unique even if two products share a kind
        if p["id"] in ids:
            p["id"] = f"{p['id']}{p['printful_product_id']}"
        ids.add(p["id"])
    out.sort(key=lambda p: (p["order"], p["price_cents"], p["name"]))
    return {"source": "printful", "fetched_at": int(time.time()), "currency": "usd",
            "limits": {"max_lines": 8, "max_qty_per_line": 10}, "products": out}


# ------------------------------------------------------------------ cache
_STATE = {"data": None, "loaded_at": 0.0, "next_try": 0.0, "refreshing": False, "source": None}
_LOCK = threading.Lock()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("products") else None
    except (OSError, ValueError):
        return None


def _initial():
    """First catalog for this process: the disk cache if it is there, else the committed snapshot."""
    d = _read_json(cache_path()) if live_enabled() else None
    if d:
        return d, "disk", float(d.get("fetched_at") or 0)
    d = _read_json(SNAPSHOT_PATH) or {"products": [], "limits": {}}
    return d, "snapshot", 0.0


def refresh(session=requests):
    """Fetch from Printful now. Returns True on success; on failure keeps the last good catalog."""
    try:
        d = fetch_from_printful(session)
    except Exception as e:          # network, JSON, Printful errors: never break the page
        log.warning("printful catalog refresh failed: %s", e)
        with _LOCK:
            _STATE["next_try"] = time.time() + retry_after()
            _STATE["refreshing"] = False
        return False
    with _LOCK:
        _STATE.update(data=d, loaded_at=time.time(), next_try=0.0, refreshing=False, source="printful")
    try:
        tmp = cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, cache_path())
    except OSError:
        pass
    return True


def get():
    """The catalog to serve right now. Never blocks on Printful."""
    start = False
    with _LOCK:
        if _STATE["data"] is None:
            d, src, at = _initial()
            _STATE.update(data=d, source=src, loaded_at=at)
        now = time.time()
        stale = now - _STATE["loaded_at"] > ttl()
        if stale and live_enabled() and not _STATE["refreshing"] and now >= _STATE["next_try"]:
            _STATE["refreshing"] = True
            start = True
        data = _STATE["data"]
    if start:
        threading.Thread(target=refresh, name="printful-catalog", daemon=True).start()
    return data


def source():
    return _STATE["source"]


def reset():
    """Forget everything (tests)."""
    with _LOCK:
        _STATE.update(data=None, loaded_at=0.0, next_try=0.0, refreshing=False, source=None)

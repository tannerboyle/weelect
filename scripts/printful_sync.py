#!/usr/bin/env python3
"""Printful helper for the We Elect shop. Read-only: it never creates, confirms or changes a Printful order or product.

The live site reads products straight from the Printful store (printful_store.py, cached). This script refreshes the
committed fallback copy and the mirrored mockup images, and prints what Printful has, so changes can be reviewed in git.

Needs PRINTFUL_API_TOKEN (and PRINTFUL_STORE_ID only for an account-level token):
  python scripts/printful_sync.py --show       list the store's products, variants and retail prices as Printful returns them
  python scripts/printful_sync.py --snapshot   write data/shop_products.json and mirror mockups into static/shop/pf/ (needs Pillow)
  python scripts/printful_sync.py --stores     list the stores the token can see (to find PRINTFUL_STORE_ID)
  python scripts/printful_sync.py --rates      Printful's STANDARD shipping rates to a sample address for common carts
"""
import argparse, hashlib, io, json, os, sys
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import printful_store as pfs  # noqa: E402

API = "https://api.printful.com"


def need_token():
    if not os.environ.get("PRINTFUL_API_TOKEN", "").strip():
        sys.exit("Set PRINTFUL_API_TOKEN first (Printful Developer Portal -> private token for the We Elect store).")


def money(c):
    return f"${c/100:,.2f}"


def cmd_show(a):
    need_token()
    cat = pfs.fetch_from_printful()
    for p in cat["products"]:
        print(f"\n{p['name']}  (Printful product {p['printful_product_id']}, cart id '{p['id']}')")
        lo, hi = p["price_cents"], p["price_max_cents"]
        print(f"  price {money(lo)}" + ("" if lo == hi else f" to {money(hi)}") + f"; {len(p['variants'])} variants")
        for c in p["colors"]:
            sizes = [s for s in p["sizes"] if f"{c['id']}:{s}" in p["variants"]]
            pr = sorted({p['variants'][f"{c['id']}:{s}"]['price_cents'] for s in sizes})
            print(f"  {c['name']:<16} {c['swatch']}  sizes {', '.join(sizes)}  {'/'.join(money(x) for x in pr)}  image {c['image']}")


def mirror(url):
    """Download a Printful mockup, shrink it to 640px JPEG on cream, save under static/shop/pf/. Returns True if saved."""
    from PIL import Image
    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
    path = os.path.join(pfs.LOCAL_IMG_DIR, name)
    if os.path.exists(path):
        return True
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content)).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    im = bg.convert("RGB")
    im.thumbnail((640, 640))
    os.makedirs(pfs.LOCAL_IMG_DIR, exist_ok=True)
    im.save(path, "JPEG", quality=84, optimize=True, progressive=True)
    return True


def cmd_snapshot(a):
    need_token()
    cat = pfs.fetch_from_printful()
    urls = set()
    for p in cat["products"]:
        for u in [p.get("image")] + [c.get("image") for c in p["colors"]]:
            if u and u.startswith("http"):
                urls.add(u)
    for u in sorted(urls):
        mirror(u)
        print("mirrored", u)
    cat = pfs.fetch_from_printful()          # again, now that local copies exist the catalog points at them
    cat["_comment"] = ("Fallback snapshot of the Printful store, written by scripts/printful_sync.py --snapshot. The live site "
                       "reads Printful directly (cached); this copy is served only when Printful has not answered yet or is down. "
                       "Do not edit prices here: change them in Printful, then re-run the snapshot.")
    with open(pfs.SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {pfs.SNAPSHOT_PATH}: {len(cat['products'])} products")


def cmd_stores(a):
    need_token()
    r = requests.get(f"{API}/stores", headers=pfs.headers(), timeout=30)
    print(r.status_code)
    for s in (r.json().get("result") or []):
        print(f"{s.get('id')}  {s.get('name')}  ({s.get('type')})")


def cmd_rates(a):
    need_token()
    cat = json.load(open(pfs.SNAPSHOT_PATH))
    first = {p["id"]: next(iter(p["variants"].values()))["printful_variant_id"] for p in cat["products"]}
    carts = [(f"1 {k}", [(v, 1)]) for k, v in first.items()]
    carts.append(("one of each", [(v, 1) for v in first.values()]))
    if "tee" in first:
        carts.append(("2 tees", [(first["tee"], 2)]))
    rec = {"country_code": "US", "state_code": "WA", "city": "Olalla", "zip": "98359", "address1": "PO Box 1042"}
    for name, its in carts:
        r = requests.post(f"{API}/shipping/rates", headers=dict(pfs.headers(), **{"Content-Type": "application/json"}), timeout=30,
                          data=json.dumps({"recipient": rec, "items": [{"variant_id": v, "quantity": q} for v, q in its], "currency": "USD"}))
        res = r.json().get("result") if r.ok else None
        print(f"{name:<14}", r.status_code, [(x.get("id"), x.get("rate")) for x in res] if res else r.text[:200])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f in ("show", "snapshot", "stores", "rates"):
        ap.add_argument("--" + f, action="store_true")
    a = ap.parse_args()
    if a.snapshot: cmd_snapshot(a)
    elif a.show: cmd_show(a)
    elif a.stores: cmd_stores(a)
    elif a.rates: cmd_rates(a)
    else: ap.print_help()

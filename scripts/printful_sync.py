#!/usr/bin/env python3
"""Printful helper for data/shop_products.json.

Catalog lookups use Printful's public catalog and need no account or token:
  python scripts/printful_sync.py --product 71 --color Navy "Soft Cream" --size S M L XL 2XL     list variants (id, color, size, base price)
  python scripts/printful_sync.py --sync                      check every variant id in data/shop_products.json against the live catalog
  python scripts/printful_sync.py --sync --write              ...and fix any ids that changed (writes the JSON file)

These need PRINTFUL_API_TOKEN (and PRINTFUL_STORE_ID for an account-level token):
  python scripts/printful_sync.py --stores                    list the stores the token can see (to find PRINTFUL_STORE_ID)
  python scripts/printful_sync.py --rates                     Printful's shipping rates for a sample U.S. address, to set the flat rates
"""
import argparse, json, os, sys
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "shop_products.json")
API = "https://api.printful.com"


def catalog_variants(product_id):
    r = requests.get(f"{API}/products/{product_id}", timeout=30)
    r.raise_for_status()
    res = r.json()["result"]
    return res["product"], res["variants"]


def auth_headers():
    tok = os.environ.get("PRINTFUL_API_TOKEN", "").strip()
    if not tok:
        sys.exit("Set PRINTFUL_API_TOKEN first (Printful dashboard -> Settings -> API).")
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    if os.environ.get("PRINTFUL_STORE_ID"):
        h["X-PF-Store-Id"] = os.environ["PRINTFUL_STORE_ID"].strip()
    return h


def cmd_list(a):
    prod, vs = catalog_variants(a.product)
    print(f"{prod['title']} (catalog product {prod['id']}); {len(vs)} variants")
    for v in sorted(vs, key=lambda v: (v["color"], v["id"])):
        if a.color and v["color"] not in a.color:
            continue
        if a.size and v["size"] not in a.size:
            continue
        print(f"{v['id']:>7}  {v['color']:<14} {v['size']:<9} base ${v['price']:<7} {'in stock' if v['in_stock'] else 'OUT OF STOCK'}")


def cmd_sync(a):
    data = json.load(open(PATH))
    changed = 0
    for p in data["products"]:
        prod, vs = catalog_variants(p["printful"]["catalog_product_id"])
        by = {(v["color"], v["size"]): v for v in vs}
        for key, var in p["variants"].items():
            cid, size = key.split(":", 1)
            pc = next(c["printful_color"] for c in p["colors"] if c["id"] == cid)
            live = by.get((pc, size))
            if not live:
                print(f"MISSING  {p['id']} {key}: no catalog variant for {pc} / {size}")
                continue
            note = "ok" if var.get("printful_variant_id") == live["id"] else f"CHANGED {var.get('printful_variant_id')} -> {live['id']}"
            stock = "" if live["in_stock"] else "  (out of stock at Printful)"
            print(f"{note:<22} {p['id']:<4} {key:<16} id {live['id']} base ${live['price']}{stock}")
            if note != "ok":
                changed += 1
                if a.write:
                    var["printful_variant_id"] = live["id"]
    if a.write and changed:
        json.dump(data, open(PATH, "w"), indent=1)
        print(f"wrote {PATH}")
    elif changed:
        print("Run again with --write to update the file.")


def cmd_stores(a):
    r = requests.get(f"{API}/stores", headers=auth_headers(), timeout=30)
    print(r.status_code)
    for s in (r.json().get("result") or []):
        print(f"{s.get('id')}  {s.get('name')}  ({s.get('type')})")


def cmd_rates(a):
    data = json.load(open(PATH))
    items = []
    for p in data["products"]:
        key = next(iter(p["variants"]))
        items.append({"variant_id": p["variants"][key]["printful_variant_id"], "quantity": 1})
    for n, its in (("1 item", items[:1]), ("2 different items", items[:2])):
        r = requests.post(f"{API}/shipping/rates", headers=auth_headers(), timeout=30,
                          data=json.dumps({"recipient": {"country_code": "US", "state_code": "TX", "city": "Austin", "zip": "78701", "address1": "1 Main St"},
                                           "items": its, "currency": "USD"}))
        print(n, r.status_code, [(x.get("id"), x.get("name"), x.get("rate")) for x in (r.json().get("result") or [])] if r.ok else r.text[:200])
    sh = data["shipping"]
    print(f"Your flat rate: {sh['first_item_cents']/100:.2f} first item + {sh['additional_item_cents']/100:.2f} each additional")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--product", type=int, help="catalog product id to list (tee 71, dad hat 206)")
    ap.add_argument("--color", nargs="*")
    ap.add_argument("--size", nargs="*")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--stores", action="store_true")
    ap.add_argument("--rates", action="store_true")
    a = ap.parse_args()
    if a.stores: cmd_stores(a)
    elif a.rates: cmd_rates(a)
    elif a.sync: cmd_sync(a)
    elif a.product: cmd_list(a)
    else: ap.print_help()

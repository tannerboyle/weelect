# We Elect: Support page and shop setup

This is everything Bob needs to do to turn the shop on. Nothing here needs code changes. Until the environment variables below are set, `/support` shows the thank-you page, the Ko-fi button and a "Coming soon" shop panel, and the rest of the site is unaffected.

> Not legal or tax advice. The sales-tax and resale sections are a checklist of things to confirm with an accountant or the state's revenue department.

## How it works

```
Reader -> /support (cart lives in the browser)
       -> /shop/checkout  (server re-prices every line from data/shop_products.json,
                           creates a Stripe Checkout Session, ui_mode=embedded_page)
       -> Stripe's embedded form (card + shipping address; card data never touches weelect.org)
       -> Stripe webhook POST /stripe/webhook  (signature verified, checkout.session.completed)
       -> Printful order created as a DRAFT (confirm=false) with external_id derived from the Stripe session id
       -> Bob reviews the draft in Printful and confirms it (or set PRINTFUL_AUTO_CONFIRM=1)
```

* Code: `shop.py`; templates `support.html`, `shop_checkout.html`, `shop_success.html`, `shop_cancel.html`; scripts `static/shop.js`, `static/shop-checkout.js`, `static/kofi.js`.
* Catalog and prices: `data/shop_products.json` (prices are set on the server, never taken from the browser).
* Printful sync helper: `scripts/printful_sync.py`.
* No Stripe SDK: `shop.py` calls Stripe's REST API with `requests` and verifies webhook signatures itself, so there are no new dependencies. Stripe API version is pinned to `2026-03-25.dahlia` (`STRIPE_API_VERSION` overrides it; `STRIPE_UI_MODE` overrides `embedded_page`, which older API versions call `embedded`).
* The app sends **no email**. Stripe emails the receipt; Printful emails tracking to the customer (we pass their email to Printful for that).

## Environment variables (Render -> weelect service -> Environment)

| Variable | Needed for | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | checkout | `sk_test_...` first, `sk_live_...` later. A restricted key with Checkout Sessions read/write also works. |
| `STRIPE_PUBLISHABLE_KEY` | checkout | `pk_test_...` / `pk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | webhook + opening the shop | `whsec_...` from the webhook endpoint (the shop stays "coming soon" without it) |
| `PRINTFUL_API_TOKEN` | fulfilment | Private token. Without it, paid orders are logged as `failed_permanent` for you to create by hand. |
| `PRINTFUL_STORE_ID` | fulfilment | Only for an account-level token (sent as `X-PF-Store-Id`). Find it with `scripts/printful_sync.py --stores`. |
| `PRINTFUL_AUTO_CONFIRM` | optional | `1` = send orders straight to production (Printful charges you then). Default: drafts for review. Orders whose Stripe subtotal does not match the catalog are never auto-confirmed. |
| `STRIPE_TAX` | optional | `1` = Stripe Tax (`automatic_tax`). See "Sales tax". |
| `STRIPE_TAX_RATE_ID` | optional | `txr_...` fixed rate applied to every line (manual alternative; ignored if `STRIPE_TAX=1`). |
| `STRIPE_TAX_CODE` | optional | Default Stripe tax code for products (otherwise your Stripe account default is used). |
| `SELLER_NAME` | optional | Seller named on the page (default `Brunner Creative`). Set to the legal name Bob sells under. |
| `SHOP_SECRET` | optional | Signing key for CSRF tokens. Defaults to a hash of `STRIPE_SECRET_KEY`. |
| `SHOP_ORDER_LOG` | optional | JSONL path (default `/tmp/weelect_shop_orders.jsonl`; Render's disk is ephemeral, the real record is the `SHOP_ORDER` lines in the Render logs). |
| `SHOP_CSP_REPORT_ONLY` | debugging | `1` = send the shop pages' Content-Security-Policy as report-only. |
| `SITE_URL` | already used | Must be `https://weelect.org` in production: it builds the Stripe `return_url` and the Printful print-file URLs. |

## 1. Stripe

1. Create an account at <https://dashboard.stripe.com/register> as the seller (Bob / Brunner Creative). Complete business details and bank account; you can build and test in **test mode** (toggle top right) before activating.
2. Developers -> API keys: copy the **test** publishable and secret keys into Render as above.
3. Developers -> Webhooks (Event destinations) -> add an endpoint:
   * URL: `https://weelect.org/stripe/webhook`
   * Events: `checkout.session.completed` and `checkout.session.async_payment_succeeded`
   * API version: the default is fine; the handler reads shipping from either `shipping_details` or `collected_information.shipping_details`.
   * Copy the **signing secret** (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
4. Settings -> Emails (or Customer emails): turn on **successful payment receipts** so customers get a receipt (the app sends none).
5. Settings -> Payment methods: leave cards/wallets on. For Apple Pay, add the domain `weelect.org` under Payment method domains.
6. Optional but recommended: Settings -> Branding (logo/colours) so the embedded form matches the site.
7. Test mode only first. When ready for real sales: finish account activation, then create a **live** webhook endpoint (live mode has its own secret) and swap all three Stripe env vars to live values.

## 2. Printful

1. Create an account at <https://www.printful.com/>.
2. Stores -> Add store -> choose **Manual order platform / API** (the integration for orders sent through the API). One store is enough.
3. Billing: add a payment method (card). Printful charges it for the item cost plus shipping **only when an order is confirmed**; drafts cost nothing.
4. Developers (<https://developers.printful.com/>) -> create a **private token** for that store with scopes for orders (read/write), store information and catalog (read). A store-level token needs no store id; an account-level token also needs `PRINTFUL_STORE_ID`.
5. Settings -> Branding / Packing slip: set your logo, return address and packing-slip message so the parcel shows We Elect rather than Printful.
6. Run `python scripts/printful_sync.py --sync` (no token needed) to confirm every variant id in `data/shop_products.json` still matches Printful's public catalog (`--write` fixes drifted ids, `--product 71 --color Navy` lists variants). The ids in the file today were read from the catalog (tee: Bella+Canvas 3001, product 71, Navy and Soft Cream, S-2XL; dad hat: Yupoong 6245CM, product 206, Navy).
7. **Review the artwork.** The print files in `static/shop/print/` are generated from the logo SVG and are placeholders sized for the print areas (tee 1800x2400 px, hat 1000x1000 px). Printful digitises hat embroidery from the PNG and may need a simplified design: look at the draft's mockup in Printful (Orders -> Drafts) before the first confirm and replace the PNGs if needed (keep the file names, or edit `files` in the JSON).
8. Check shipping: `PRINTFUL_API_TOKEN=... python scripts/printful_sync.py --rates` prints Printful's rates; compare to `shipping` in `data/shop_products.json` (flat $5.95 first item + $2.00 each additional is a **placeholder**, not Printful's rate) and edit.

Margin check (illustrative, from the public catalog on 2026-10-28; recheck before launch): tee base cost $11.92 (2XL $13.92), dad hat $14.94, at $28 retail. Subtract Printful's shipping charge, Stripe's card fee (check your Stripe pricing; commonly 2.9% + 30 cents in the U.S.), and any tax you must remit.

## 3. Resale and sales-tax notes (confirm with an accountant)

* **Bob is the seller of record.** Stripe Checkout takes payment for Bob's Stripe account; Printful is a supplier that prints and ships. The page names the seller (`SELLER_NAME`), says We Elect is not a charity, and says purchases and gifts are not tax-deductible. Do not add any charity or deductibility language.
* **Sales tax on what you sell.** Selling taxable goods to buyers in a state can require you to register, collect and remit sales tax there (thresholds and rules vary by state; many states use "economic nexus" thresholds). Check your home state's requirements first (many states require a seller's permit before the first sale).
  * Option A, Stripe Tax: Stripe Dashboard -> Tax -> set your head-office address, set a default product tax category (apparel/clothing), add the states where you are registered, then set `STRIPE_TAX=1`. Stripe calculates and collects tax per ship-to address; **you** still register with each state and file returns (Stripe Tax can help but does not file unless you use its filing partner). Stripe Tax may charge a per-transaction fee.
  * Option B, manual: create one tax rate in Stripe (Product catalog -> Tax rates) and put its `txr_` id in `STRIPE_TAX_RATE_ID`. It applies the **same** rate to every order, so it only fits you if you collect tax in one place; tax by address needs Stripe Tax.
  * Option C: collect no tax at launch only if you have confirmed with your state that you do not owe it yet. Keep records either way.
* **Printful and resale.** Printful may charge sales tax on orders shipped to states where it has to collect tax, unless you give it a valid resale/exemption certificate for that state (Printful dashboard -> Settings -> Billing/Tax). Collecting tax from customers *and* paying it to Printful can double-tax you unless the certificate is on file; ask your accountant.
* **Shipping to U.S. addresses only** (`shipping.countries` in the JSON). Adding countries means customs forms, VAT and different shipping rates.
* **Policies to decide before launch** (not in the code): returns/exchanges (Printful products are made to order), a contact address for order problems (`CORRECTIONS_EMAIL` shows on the About page; the support page links to it), and how long you will take to resolve problems.

## 4. Test it in test mode (no real money)

1. In Render set the test Stripe vars (above), `PRINTFUL_API_TOKEN` and `SITE_URL=https://weelect.org`, deploy this branch's code to a preview/staging service or to production while still in test mode.
2. Open `/support#shop`, add a tee and a hat, press **Check out**.
3. Pay with Stripe test cards (any future expiry, any CVC, any postal code): `4242 4242 4242 4242` succeeds; `4000 0025 0000 3155` asks for 3-D Secure; `4000 0000 0000 9995` is declined.
4. Check, in order: the success page appears; Stripe -> Webhooks shows `200`; Render logs contain `SHOP_ORDER {... "state":"created" ...}`; Printful -> Orders -> **Drafts** has the order with the right items, variants and address.
5. Replay: Stripe -> the event -> **Resend**. Logs say `duplicate` (or Printful reports the existing external id) and no second draft appears.
6. Check the failure paths: remove `PRINTFUL_API_TOKEN` and pay again; the log shows `failed_permanent`/"create order by hand" and the customer is unaffected.
7. Delete the test drafts in Printful. Never confirm a test-mode draft unless you want Printful to produce it (Printful bills you for confirmed orders even when the Stripe payment was a test).
8. Go live: activate Stripe, create the live webhook, swap the three Stripe env vars, place one small real order yourself, review the Printful draft and confirm it. After a few clean orders, consider `PRINTFUL_AUTO_CONFIRM=1`.

Local testing: `STRIPE_SECRET_KEY=sk_test_... STRIPE_PUBLISHABLE_KEY=pk_test_... STRIPE_WEBHOOK_SECRET=$(stripe listen --print-secret) SITE_URL=http://localhost:5000 python app.py`, then `stripe listen --forward-to localhost:5000/stripe/webhook`. (`stripe trigger` events carry no cart, so the app ignores them; use a real test checkout.) Print files are fetched by Printful from `SITE_URL`, so a localhost draft will not have viewable artwork.

Automated tests (no keys, no network): `python -m unittest discover -s tests -v`.

## 5. Running it

* **Where orders are recorded:** Render logs (`SHOP_ORDER {...}` lines: session id, state, Printful id, cart, no addresses) plus Stripe and Printful dashboards. Search the logs for `failed_permanent` or `retrying` to find orders that need a human.
* **Refunds:** refund in Stripe; if the Printful order is still a draft, delete it, otherwise cancel it in Printful before production starts.
* **Changing products or prices:** edit `data/shop_products.json` (set `available` to `false` to hide a product). Prices are in cents.
* **Webhook retries:** if Printful is down (5xx/429/network) the webhook returns 500 and Stripe retries for up to three days; Printful's per-store `external_id` makes retries safe.

## 6. Security notes

* Card data goes from Stripe's iframe straight to Stripe; weelect.org only sees the client secret and the finished session.
* Cart endpoints: signed CSRF token + matching HttpOnly `SameSite=Strict` cookie (path `/shop`, 4-hour life) + `Origin` check; per-IP rate limits (checkout 12/min, webhook 240/min); request-size cap; generic error messages (details only in server logs); the browser never sets a price.
* Webhook: HMAC-SHA256 signature verification with a 5-minute timestamp tolerance, constant-time compare, body-size cap, only sessions we created (`metadata.source=weelect-shop`), cart re-validated against the catalog, never auto-confirmed if the subtotal differs.
* `/support` and `/shop/*` get a stricter CSP than the rest of the site: scripts from `self`, `js.stripe.com`; frames only from Stripe and Ko-fi; `connect-src` only `self` and Stripe; `form-action 'self'` (embedded Checkout needs no form posts to Stripe); `frame-ancestors 'none'`. They also send `Permissions-Policy: payment=(self "https://js.stripe.com" "https://checkout.stripe.com" "https://ko-fi.com")` so Apple/Google Pay can work; every other page keeps `payment=()`. These pages are `Cache-Control: no-store`.
* If the checkout form or Ko-fi frame fails to appear in a real browser, open the console for CSP messages and try `SHOP_CSP_REPORT_ONLY=1` while you adjust `shop_csp()` in `shop.py`.
* The Ko-fi form loads only after the visitor clicks "Show the Ko-fi form here"; the "Give through Ko-fi" link opens Ko-fi in a new tab.
* Rate-limit counters and the order lock are per worker process (fine at this size); Printful's `external_id` is the cross-process idempotency guard.

## 7. What was not tested against live services

Everything is tested with mocked HTTP (see `tests/test_shop.py`): webhook signatures, Printful order payload, idempotency and retry behaviour, CSRF, rate limiting, cart validation and the "coming soon" mode. **Not exercised against the real Stripe/Printful/Ko-fi services** (no keys were available): the embedded Checkout form itself (`createEmbeddedCheckoutPage`, with `initEmbeddedCheckout` as a fallback), Stripe Tax, Apple/Google Pay in the iframe, Printful accepting the print-file URLs and embroidery file for the hat, and the Ko-fi iframe (Ko-fi's page returned a Cloudflare 403 to our headless test browser, though its embed URL sends no `X-Frame-Options`). Run section 4 once in test mode before going live.

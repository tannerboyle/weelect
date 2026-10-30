# We Elect: Support page and shop setup

This is everything Bob needs to do to turn the shop on. Nothing here needs code changes. Until the PayPal environment variables below are set, `/support` shows the thank-you page, the Ko-fi button and a "Coming soon" shop panel, and the rest of the site is unaffected.

> Not legal or tax advice. The sales-tax and resale sections are a checklist of things to confirm with an accountant or the state's revenue department.

## How it works

```
Reader -> /support (cart lives in the browser)
       -> /shop/checkout   our own page: shipping-address form, then PayPal's buttons drawn on the same page
       -> POST /shop/quote            server prices the cart from data/shop_products.json + SHOP_TAX_RATES (display only)
       -> PayPal button / "Debit or Credit Card" button
            createOrder -> POST /shop/paypal/order
                 server re-prices every line, builds an itemized PayPal order (Orders API v2):
                 items + item_total + shipping + tax_total, our address with shipping_preference SET_PROVIDED_ADDRESS
            buyer approves in PayPal's popup (PayPal account) or in PayPal's inline card form (no account needed)
            onApprove  -> POST /shop/paypal/order/<id>/capture
                 server captures; ONLY if PayPal reports the capture COMPLETED does it create the Printful order
       -> /shop/success?order=<id>  (on weelect.org)
Backstop: PayPal webhook POST /paypal/webhook (verified with PayPal's verify-webhook-signature API)
       PAYMENT.CAPTURE.COMPLETED -> create the Printful order if it is not there yet (e.g. a capture that was PENDING)
       CHECKOUT.ORDER.APPROVED  -> capture it ourselves if the buyer's browser never did (closed tab), then fulfil
Printful order = DRAFT by default, external_id "pp" + PayPal order id, so it can only ever be created once.
```

* **Nobody leaves weelect.org.** The buttons and the card form are PayPal iframes on our page. Paying with a PayPal account opens PayPal's login in a small popup window over our page (that is how PayPal's buttons work; the page itself never redirects). Paying by card stays inline.
* **Shipping address:** collected by our own form, sent to PayPal as the order's shipping address (`SET_PROVIDED_ADDRESS`, so the buyer cannot change it inside PayPal). Shipping and tax are computed for that exact address, and the Printful recipient is read back from PayPal's captured order, so Printful always ships to the address that was paid for. If the buyer edits the address after the quote, the buttons hide until they press "Continue to payment" again.
* **Prices never come from the browser.** The browser sends only product, color, size and quantity. The server builds the PayPal order from the catalog; at capture the server re-reads the items from PayPal (sku = `product.color.size`), re-validates them and checks the captured amount equals the catalog total. An order whose amount does not match still becomes a Printful **draft** for you to look at, but is never auto-confirmed.
* Code: `shop.py`; templates `support.html`, `shop_checkout.html`, `shop_success.html`; scripts `static/shop.js`, `static/shop-checkout.js`, `static/kofi.js`.
* Catalog and prices: `data/shop_products.json`. Printful sync helper: `scripts/printful_sync.py`.
* No PayPal server SDK: `shop.py` calls PayPal's REST API with `requests` (OAuth client-credentials token, cached), so there are no new dependencies.
* The app sends **no email**. PayPal emails the buyer a receipt; Printful emails tracking (we pass the buyer's PayPal email to Printful for that).

## Environment variables (Render -> weelect service -> Environment)

| Variable | Needed for | Notes |
|---|---|---|
| `PAYPAL_CLIENT_ID` | checkout | From your PayPal REST app (sandbox first, then live). Public: it appears in the page. |
| `PAYPAL_CLIENT_SECRET` | checkout | From the same app. Secret: only ever in Render. |
| `PAYPAL_WEBHOOK_ID` | webhook + opening the shop | The **Webhook ID** PayPal shows for the `/paypal/webhook` URL. The shop stays "coming soon" without it. |
| `PAYPAL_ENV` | checkout | `sandbox` (default) or `live`. Must match the app the id/secret came from. |
| `PRINTFUL_API_TOKEN` | fulfilment | Private token. Without it, paid orders are logged as `failed_permanent` for you to create by hand. |
| `PRINTFUL_STORE_ID` | fulfilment | Only for an account-level token (sent as `X-PF-Store-Id`). Find it with `scripts/printful_sync.py --stores`. |
| `PRINTFUL_AUTO_CONFIRM` | optional | `1` = send orders straight to production (Printful charges you then). Default: drafts for review. Orders whose paid amount does not match the catalog are never auto-confirmed. |
| `SHOP_TAX_RATES` | optional | Sales tax per ship-to state, as JSON percentages. See "Sales tax". Default: no tax collected anywhere. |
| `SELLER_NAME` | optional | Seller named on the page (default `Brunner Creative`). Set to the legal name Bob sells under. |
| `SHOP_SECRET` | optional | Signing key for CSRF tokens. Defaults to a hash of `PAYPAL_CLIENT_SECRET`. |
| `SHOP_ORDER_LOG` | optional | JSONL path (default `/tmp/weelect_shop_orders.jsonl`; Render's disk is ephemeral, the real record is the `SHOP_ORDER` lines in the Render logs). |
| `SHOP_CSP_REPORT_ONLY` | debugging | `1` = send the shop pages' Content-Security-Policy as report-only. |
| `SITE_URL` | already used | Must be `https://weelect.org` in production: it builds the Printful print-file URLs. |

The old Stripe variables (`STRIPE_*`) are no longer used; delete them from Render if any were added.

## 1. PayPal

1. **Business account.** Sign up at <https://www.paypal.com/us/business> for a **Business** account as the seller (Bob / Brunner Creative), or upgrade a personal account. Confirm the email, link and confirm a bank account, and finish any identity or business-verification steps PayPal asks for. Under Account Settings -> Business information, set the customer-service email and the name buyers see.
2. **Developer app (credentials).** Log in at <https://developer.paypal.com/dashboard/> with the same login. Under **Apps & Credentials**:
   * With the toggle on **Sandbox**, open the default app or **Create App** (type: Merchant). Copy its **Client ID** and **Secret** into `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`, and set `PAYPAL_ENV=sandbox`.
   * The sandbox also gives you test accounts (Testing Tools -> Sandbox Accounts): a *business* account that receives the money and a *personal* account to pay with.
3. **Webhook.** In the same app, under **Webhooks** -> **Add Webhook**:
   * URL: `https://weelect.org/paypal/webhook`
   * Events: `Checkout order approved` (`CHECKOUT.ORDER.APPROVED`), `Checkout order completed` (`CHECKOUT.ORDER.COMPLETED`), `Payment capture completed` (`PAYMENT.CAPTURE.COMPLETED`), `Payment capture denied` (`PAYMENT.CAPTURE.DENIED`), `Payment capture refunded` (`PAYMENT.CAPTURE.REFUNDED`), `Payment capture reversed` (`PAYMENT.CAPTURE.REVERSED`). (Refund/reversal/denial events are only logged so you notice them; you handle the Printful side by hand.)
   * Copy the **Webhook ID** it shows into `PAYPAL_WEBHOOK_ID`. Every event is checked with PayPal's verify-webhook-signature API against this id; events that fail are rejected.
4. **Card payments without an account** ("Debit or Credit Card" button) are on by default for U.S. business accounts. If the card button does not appear, check Account Settings -> Website payments -> **PayPal account optional** is On.
5. **Live.** When sandbox testing (section 4) passes: switch the developer dashboard toggle to **Live**, create a live app, add the same webhook URL and events to it, and set `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` to the **live** values and `PAYPAL_ENV=live` at the same time (a sandbox id with `PAYPAL_ENV=live`, or the reverse, makes checkout fail with a generic error).

## 2. Printful

1. Create an account at <https://www.printful.com/>.
2. Stores -> Add store -> choose **Manual order platform / API** (the integration for orders sent through the API). One store is enough.
3. Billing: add a payment method (card). Printful charges it for the item cost plus shipping **only when an order is confirmed**; drafts cost nothing.
4. Developers (<https://developers.printful.com/>) -> create a **private token** for that store with scopes for orders (read/write), store information and catalog (read). A store-level token needs no store id; an account-level token also needs `PRINTFUL_STORE_ID`.
5. Settings -> Branding / Packing slip: set your logo, return address and packing-slip message so the parcel shows We Elect rather than Printful.
6. Run `python scripts/printful_sync.py --sync` (no token needed) to confirm every variant id in `data/shop_products.json` still matches Printful's public catalog (`--write` fixes drifted ids, `--product 71 --color Navy` lists variants). The ids in the file today were read from the catalog (tee: Bella+Canvas 3001, product 71, Navy and Soft Cream, S-2XL; dad hat: Yupoong 6245CM, product 206, Navy).
7. **Review the artwork.** The print files in `static/shop/print/` are generated from the logo SVG and are placeholders sized for the print areas (tee 1800x2400 px, hat 1000x1000 px). Printful digitises hat embroidery from the PNG and may need a simplified design: look at the draft's mockup in Printful (Orders -> Drafts) before the first confirm and replace the PNGs if needed (keep the file names, or edit `files` in the JSON).
8. Check shipping: `PRINTFUL_API_TOKEN=... python scripts/printful_sync.py --rates` prints Printful's rates; compare to `shipping` in `data/shop_products.json` (flat $5.95 first item + $2.00 each additional is a **placeholder**, not Printful's rate) and edit.

Margin check (illustrative, from the public catalog on 2026-10-28; recheck before launch): tee base cost $11.92 (2XL $13.92), dad hat $14.94, at $28 retail. Subtract Printful's shipping charge, PayPal's fee and any tax you must remit. PayPal's U.S. fee page (last updated Oct 1, 2026) lists both PayPal Checkout and PayPal Guest Checkout (paying by card without an account) at 3.49% + $0.49 per sale, about $1.47 on a $28 item; recheck at <https://www.paypal.com/us/business/paypal-business-fees>.

## 3. Sales tax, resale and policies (confirm with an accountant)

* **Bob is the seller of record.** PayPal takes payment for Bob's PayPal Business account; Printful is a supplier that prints and ships. The page names the seller (`SELLER_NAME`), says We Elect is not a charity, and says purchases and gifts are not tax-deductible. Do not add any charity or deductibility language.
* **PayPal does not work out sales tax.** The shop adds tax only from `SHOP_TAX_RATES`, a JSON object of ship-to state -> percent:
  * `{"WA": 10.25}` charges 10.25% of the item total on orders shipped to Washington, and nothing anywhere else.
  * `{"TX": {"rate": 8.25, "shipping": true}}` also taxes the shipping charge (some states tax shipping, some do not).
  * Unlisted states are charged no tax. Unset or invalid JSON means no tax anywhere (bad entries are skipped and logged, never guessed).
  * The rate is a single number per state. Many states add city/county rates on top, which vary by address; a flat state number is only right if that is what your state tells you to collect (for example, some states let remote sellers use one rate). **Bob must confirm, with his state's revenue department or an accountant, where he has to register and collect and at what rate, before turning the shop on.** If he needs exact rates by street address, that needs a tax service (for example TaxJar or Avalara), which this code does not include.
  * The tax is itemized on the checkout page and in the PayPal order (`tax_total`), and the fine print on `/support` lists the states you collect for.
* **Seller's permit.** Many states require a seller's permit before the first sale, even if little or no tax is due. Selling into other states can create obligations there once you pass their "economic nexus" thresholds.
* **Printful and resale.** Printful may charge sales tax on orders shipped to states where it has to collect tax, unless you give it a valid resale/exemption certificate for that state (Printful dashboard -> Settings -> Billing/Tax). Collecting tax from customers *and* paying it to Printful can double-tax you unless the certificate is on file; ask your accountant.
* **Shipping to U.S. addresses only** (50 states + DC; the form offers nothing else). Adding countries means customs forms, VAT and different shipping rates.
* **Policies to decide before launch** (not in the code): returns/exchanges (Printful products are made to order), a contact address for order problems (`CORRECTIONS_EMAIL` shows on the About page; the support page links to it), and how long you will take to resolve problems.

## 4. Test it in the PayPal sandbox (no real money)

1. In Render (a preview/staging service, or production while still on sandbox credentials) set the **sandbox** `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` (from a sandbox webhook pointing at that service's `/paypal/webhook`), `PAYPAL_ENV=sandbox`, `PRINTFUL_API_TOKEN` and `SITE_URL`, then deploy this branch.
2. Open `/support#shop`, add a tee and a hat, press **Check out**, enter a U.S. address, press **Continue to payment**. Check the summary: items, shipping, tax (if you set `SHOP_TAX_RATES`), total.
3. **PayPal button:** log in with the sandbox *personal* account and pay. **Card button:** click "Debit or Credit Card" and use a test card from developer.paypal.com -> Testing Tools -> **Card Testing** (a generated Visa works with any future expiry and any 3-digit CVV).
4. Check, in order: the "Thank you" page appears on weelect.org; Render logs show `SHOP_ORDER {... "state":"created" ...}`; Printful -> Orders -> **Drafts** has the order with the right items, variants and the address you typed; the sandbox business account (log in at <https://www.sandbox.paypal.com>) shows the payment with the itemized amounts.
5. Webhook: developer dashboard -> your sandbox app -> Webhooks -> **Webhook events** shows the deliveries with `200`. Use **Resend** on a `PAYMENT.CAPTURE.COMPLETED` event: logs say `duplicate` and no second draft appears.
6. Failure paths:
   * Decline: in the card form use a card number from Card Testing set to fail, or (PayPal button) use the sandbox "negative testing" options. The page should say the payment was declined and let the buyer try again; nothing reaches Printful.
   * Remove `PRINTFUL_API_TOKEN` and pay again: the buyer still sees "Thank you", and the log shows `failed_permanent` / "create order by hand".
   * Close the tab right after approving in the PayPal popup (before the thank-you page): within a minute or so the `CHECKOUT.ORDER.APPROVED` webhook captures and creates the draft.
7. Delete the test drafts in Printful. **Never confirm a sandbox draft** unless you want Printful to produce it (Printful bills you for confirmed orders even though the PayPal payment was a sandbox test).
8. **Go live** (section 1, step 5), place one small real order yourself, review the Printful draft and confirm it. After a few clean orders, consider `PRINTFUL_AUTO_CONFIRM=1`.

Local testing: `PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... PAYPAL_WEBHOOK_ID=anything SITE_URL=http://localhost:5000 python app.py` works for the buttons and capture path (webhooks need a public URL, e.g. a tunnel, registered as a sandbox webhook). Print files are fetched by Printful from `SITE_URL`, so a localhost draft will not have viewable artwork.

Automated tests (no keys, no network; PayPal and Printful are faked): `python -m unittest discover -s tests -v`.

## 5. Running it

* **Where orders are recorded:** Render logs (`SHOP_ORDER {...}` lines: PayPal order id, capture id, state, Printful id, cart, ship-to state, amounts; no street addresses) plus the PayPal and Printful dashboards. Search the logs for `failed_permanent`, `retrying`, `pending` or `notice` (refunds, reversals) to find orders that need a human.
* **Pending payments:** PayPal sometimes holds a capture as `PENDING` (e.g. review). The buyer sees "PayPal is still confirming your payment"; nothing is sent to Printful until the `PAYMENT.CAPTURE.COMPLETED` webhook arrives.
* **Refunds:** refund in PayPal (Activity -> the payment -> Refund). If the Printful order is still a draft, delete it; otherwise cancel it in Printful before production starts.
* **Changing products or prices:** edit `data/shop_products.json` (set `available` to `false` to hide a product). Prices are in cents.
* **Webhook retries:** if Printful is down (5xx/429/network) the webhook answers 500 and PayPal redelivers (PayPal retries failed deliveries for up to three days). Printful's per-store `external_id` (`pp` + PayPal order id), checked before every create, makes retries safe across workers and restarts.

## 6. Security notes

* Card data is typed into PayPal's iframe and goes straight to PayPal; weelect.org only sees the PayPal order id and the finished order.
* Browser endpoints (`/shop/quote`, `/shop/paypal/order`, `/shop/paypal/order/<id>/capture`): signed CSRF token + matching HttpOnly `SameSite=Strict` cookie (path `/shop`, 4-hour life) + `Origin` check; per-IP rate limits (quote 30/min, create and capture 12/min each, webhook 240/min); request-size cap; generic error messages (details only in server logs); the browser never sets a price.
* Capture is idempotent (`PayPal-Request-Id: we-capture-<order id>` and `ORDER_ALREADY_CAPTURED` handling), so a double click, the browser and the webhook racing, or a retry can never charge twice or create two Printful orders. Only orders carrying our `custom_id` (`weelect-shop`) are captured or fulfilled.
* Webhook: every event is verified by PayPal's `/v1/notifications/verify-webhook-signature` against `PAYPAL_WEBHOOK_ID`; missing headers or a failed check -> 400; if PayPal's verification service cannot be reached -> 503 so PayPal retries. The handler never trusts amounts in the event: it re-reads the order from PayPal.
* `/support` and `/shop/*` get a stricter CSP than the rest of the site, matching PayPal's published CSP for the JS SDK: scripts, styles, fonts, images, XHR and frames from `https://*.paypal.com` and `https://*.paypalobjects.com`; frames also from Ko-fi; GoatCounter (`gc.zgo.at`, `weelect.goatcounter.com`) for analytics; `form-action 'self'`; `frame-ancestors 'none'` (the site still cannot be framed by anyone). Venmo and Pay Later are switched off in the SDK URL, so `*.venmo.com` is not allowed. These pages send `Permissions-Policy: payment=(self "https://www.paypal.com" "https://www.sandbox.paypal.com" "https://ko-fi.com")`; every other page keeps `payment=()`. Shop pages are `Cache-Control: no-store`.
* If the buttons or card form fail to appear in a real browser, open the console for CSP messages and try `SHOP_CSP_REPORT_ONLY=1` while you adjust `shop_csp()` in `shop.py`.
* The Ko-fi form loads only after the visitor clicks "Show the Ko-fi form here"; the "Give through Ko-fi" link opens Ko-fi in a new tab.

## 7. What was not tested against live services

Everything is tested with faked HTTP (see `tests/test_shop.py`): order building and itemized totals, tax by state, price/quantity tampering, capture outcomes (completed, pending, declined, already captured), webhook verification failures, idempotency across retries and restarts, CSRF, rate limiting and the "coming soon" mode. **Not exercised against real PayPal/Printful** (no credentials were available): a real sandbox order and capture, PayPal accepting `application_context.shipping_preference = SET_PROVIDED_ADDRESS` with the card button, the inline card form's behaviour, the exact shape of real webhook events and the verify-signature call, and Printful accepting the print-file URLs and the hat embroidery file. Run section 4 once in the sandbox before going live.

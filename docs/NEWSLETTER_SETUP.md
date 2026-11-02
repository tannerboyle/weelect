# Newsletter setup (Resend)

The signup form on weelect.org ("Get We Elect updates") is built and stays in a polite **"Newsletter coming soon"**
state until the Render environment variables below are set. Nothing leaves weelect.org: the form posts to our own
server, which talks to Resend behind the scenes.

How it works:
1. A reader enters an email address. We send one **confirmation email** (double opt-in). Nothing is saved yet.
2. They click the link and press **Yes, sign me up** on weelect.org (a button, so corporate link-scanners can't
   subscribe people). Only then is the address added to Resend as a subscribed contact in our segment.
3. You write and send issues from Resend's **Broadcasts** editor. Resend adds the unsubscribe link and handles
   unsubscribes for us.

Resend facts below were read from resend.com on 2026-11-01.

---

## 1. Create the Resend account

Sign up at <https://resend.com/signup> (free plan, no card needed).

## 2. Add the sending domain: `news.weelect.org`

Resend recommends sending from a **subdomain**, not the root domain, to keep newsletter reputation separate
("We recommend sending your emails from one or more subdomains ... to isolate your sending reputation",
<https://resend.com/docs/dashboard/domains/introduction>). Using `news.weelect.org` also means **nothing about
weelect.org's existing email changes**: the root MX records (`mx1/mx2.dreamhost.com`, which deliver the corrections
email) and the root SPF record stay exactly as they are.

In Resend: **Domains → Add Domain** → enter `news.weelect.org` → region **North Virginia (us-east-1)** (closest to
most US readers) → leave the custom Return-Path blank (default `send`).

Resend then shows a **Records** tab. **Always copy the values from that tab**; the values below show the shape only.

## 3. Add the DNS records at DreamHost

DreamHost panel → **Websites → Manage Websites** → weelect.org → **Manage** → **DNS** tab → **Add Record**.
In DreamHost's **Host** box enter only the part *before* `.weelect.org` (Resend's guide warns not to paste the full
domain).

Resend's dashboard will show one of two record sets. (Resend: "For domains created after August 2026, Resend may
generate CNAME records for sending instead of the MX and TXT pair ... Always add the exact records shown in your
domain's Records tab", <https://resend.com/docs/knowledge-base/how-do-i-avoid-conflicting-with-my-mx-records>.)

### If Resend shows TXT + MX records (classic set)

| # | DreamHost Type | DreamHost Host | Value (copy from Resend) | Purpose |
|---|---|---|---|---|
| 1 | TXT | `resend._domainkey.news` | `p=MIGf...` (long DKIM key from Resend) | DKIM signature |
| 2 | TXT | `send.news` | `v=spf1 include:amazonses.com ~all` | SPF for the bounce address |
| 3 | MX  | `send.news` | `feedback-smtp.us-east-1.amazonses.com`, priority **10** | Bounces and complaints come back to Resend |

DreamHost quirk for #3 (from Resend's DreamHost guide, <https://resend.com/docs/guides/dns/dreamhost>): DreamHost
only lets you set an MX on a host that exists as a "website". If the MX option isn't offered for `send.news`, go to
**Manage Websites → Add Website**, add `send.news.weelect.org` as a DNS-only site, click **Manage** on it,
then **Add Record → MX → Manage Custom MX** and paste the Resend value, replacing DreamHost's default.
This MX is on `send.news.weelect.org` only. **Do not edit or replace the MX records on weelect.org itself.**

### If Resend shows CNAME records (newer set)

Add each one as **Type CNAME**, Host = the name Resend shows minus `.weelect.org` (for example
`resend._domainkey.news`, `send.news`), Value = Resend's value. No MX is needed in that case. A CNAME can't share a
host with any other record, so don't add a TXT or MX on the same host.

### DMARC (recommended, add once)

| Type | Host | Value |
|---|---|---|
| TXT | `_dmarc` | `v=DMARC1; p=none;` |

This is on the root (`_dmarc.weelect.org`) and covers `news.weelect.org` too. `p=none` only monitors, so it can't
block any mail, including DreamHost's. weelect.org has no DMARC record today (checked 2026-11-01). If you'd like
weekly reports, add `rua=mailto:<an address you read>;`. Resend suggests tightening to `p=quarantine` later, once
everything passes (<https://resend.com/docs/dashboard/domains/dmarc>).

Then click **Verify DNS Records** in Resend. It often verifies in about 15 minutes; DreamHost changes can take a few
hours, and Resend allows up to 72. Resend's free checker <https://dns.email> shows whether the records are public.
In the domain's settings, I suggest leaving **open and click tracking off**, which fits our privacy promise.

## 4. Create the segment (formerly "audience")

Resend renamed Audiences to **Segments** in November 2025
(<https://resend.com/docs/dashboard/contacts/migrating-from-audiences-to-segments>).
**Contacts → Segments → Create segment** → name it `We Elect updates`. Copy its **ID**.

Optional: **Contacts → Topics → Create topic** named `We Elect updates` (default: opt-in). With a topic, Resend's
unsubscribe page lets people opt out of just this topic. Copy its ID if you make one.

## 5. Create the API key

**API Keys → Create API Key** → name `weelect-render` → permission **Full access**.
(A "Sending access" key can only send emails and **cannot add contacts**, which the signup needs:
<https://resend.com/docs/api-reference/api-keys/create-api-key>.) Copy it once; Resend won't show it again.
Don't paste it into chat; put it straight into Render.

## 6. Set the Render environment variables

Render → the weelect service → **Environment** → add:

| Key | Value | Required |
|---|---|---|
| `RESEND_API_KEY` | the `re_...` key from step 5 | yes |
| `RESEND_SEGMENT_ID` | the segment ID from step 4 (`RESEND_AUDIENCE_ID` also works) | yes |
| `SECRET_KEY` | click **Generate** in Render (a long random value) | recommended |
| `NEWSLETTER_FROM` | `We Elect <updates@news.weelect.org>` (this is the default; must be on the verified domain) | optional |
| `NEWSLETTER_REPLY_TO` | an address you read, if you'd like replies to reach you | optional |
| `NEWSLETTER_POSTAL_ADDRESS` | **TODO(Bob):** Brunner Creative LLC's physical postal address, e.g. `123 Main St, Springfield, ST 00000` (a street address, or a USPS PO box / private mailbox registered to the LLC). Shown in the confirmation-email footer; leave unset until you have it | needed before the first broadcast |
| `RESEND_TOPIC_ID` | the topic ID from step 4, if you made one | optional |
| `NEWSLETTER_DAILY_CAP` | default `80` confirmation emails a day (Resend Free allows 100 sends a day in total) | optional |

Save, then deploy. The form appears on the home page, /get-involved and /newsletter as soon as the two required
values are set. Until then, those spots say "Newsletter coming soon".

## 7. Send a broadcast

**Broadcasts → Create broadcast**:
- **From:** `We Elect <updates@news.weelect.org>`. **To:** the `We Elect updates` segment (and topic, if you made one).
- Write it in the editor, keep it short and nonpartisan.
- **Sender and postal address (CAN-SPAM).** Every broadcast is a marketing email, so its footer must name the sender
  and give a valid physical postal address. Put this line in the footer above the unsubscribe link:
  `We Elect is published by Brunner Creative LLC, PO Box 1042, Olalla, WA 98359.` (the same address as
  `NEWSLETTER_POSTAL_ADDRESS`). The footer snippet below already includes it.
- The footer must keep the unsubscribe link. Resend fills in `{{{RESEND_UNSUBSCRIBE_URL}}}` with a personal
  one-click link for each reader (<https://resend.com/docs/dashboard/broadcasts/editor>).
- Click **Test Email** to send yourself a copy first, then **Send** and slide to confirm.

### Broadcast header and footer (same look as the site and the confirmation email)

The header uses the masthead image `https://weelect.org/static/email/masthead.png` (the logo mark and the
"We Elect" wordmark, rendered from the site's own font at 3x, because email apps don't load web fonts). The
tagline and rules stay as live text. If someone's email app blocks images, they see the words "We Elect" in a
bold serif instead.

In the Broadcast editor, type `/` and choose **HTML** at the very top of the email, paste the header; add
another **HTML** block at the very bottom and paste the footer. Set **Styles → background** to `#f7f3e8`.
(If you build broadcasts with the API instead, wrap your content between the two snippets in the `html` field.)

Header:

```html
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;margin:0 auto;background:#f7f3e8;">
<tr><td style="height:6px;line-height:6px;font-size:0;background:#9b2335;" width="33%">&nbsp;</td><td style="height:6px;line-height:6px;font-size:0;background:#f7f3e8;" width="34%">&nbsp;</td><td style="height:6px;line-height:6px;font-size:0;background:#1f2f5a;" width="33%">&nbsp;</td></tr>
<tr><td colspan="3" align="center" style="background:#f7f3e8;padding:18px 20px 0;">
<a href="https://weelect.org" style="text-decoration:none;color:#000000;"><img src="https://weelect.org/static/email/masthead.png" width="300" height="69" alt="We Elect" style="display:block;margin:0 auto;width:300px;max-width:100%;height:auto;border:0;outline:none;text-decoration:none;font-family:Georgia,'Times New Roman',serif;font-size:40px;font-weight:bold;line-height:69px;letter-spacing:-1px;color:#000000;text-align:center;"></a>
<div style="font-family:Georgia,'Times New Roman',serif;font-style:italic;font-size:15px;line-height:1.4;color:#5d5f66;padding:6px 0 10px;">Know who represents you.</div>
<div style="border-top:3px solid #15171c;border-bottom:1px solid #15171c;padding:6px 0;font-family:Georgia,'Times New Roman',serif;font-size:11px;line-height:1.4;letter-spacing:2px;text-transform:uppercase;color:#15171c;">Nonpartisan civic information</div>
</td></tr>
</table>
```

Footer (keeps the CAN-SPAM sender line, postal address and Resend's one-click unsubscribe link):

```html
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;margin:0 auto;">
<tr><td align="center" style="padding:14px 28px 20px;border-top:1px solid #cfc8b4;font-family:Georgia,'Times New Roman',serif;font-size:12px;line-height:1.5;color:#5d5f66;">
<span style="color:#9b2335;letter-spacing:6px;">&#9733; &#9733; &#9733;</span><br>
We Elect is an independent, nonpartisan civic information project published by Brunner Creative LLC.<br>
Brunner Creative LLC, PO Box 1042, Olalla, WA 98359<br>
<a href="https://weelect.org" style="color:#5d5f66;">weelect.org</a> &middot; <a href="{{{RESEND_UNSUBSCRIBE_URL}}}" style="color:#5d5f66;">Unsubscribe</a>
</td></tr>
</table>
```

To change the masthead image, edit `scripts/email-masthead.html` and run
`python scripts/build_email_masthead.py` (needs Playwright and Chrome), then deploy. The image is served from
weelect.org, so it only shows in real emails after a deploy.

Optional: **Settings → Unsubscribe page** lets you brand the unsubscribe page with the logo and colours.

Housekeeping: unsubscribed contacts still count toward the contact limit until deleted. Every few months, filter
**Contacts** by **Unsubscribed** and delete them (<https://resend.com/docs/dashboard/contacts/managing-unsubscribe-list>).

---

## Free plan limits

From <https://resend.com/pricing> (read 2026-11-01):

- **Marketing (broadcasts): 1,000 contacts, $0/month.** Broadcasts aren't capped by number of sends, only by contacts,
  and don't count against the transactional limit. 3 segments on Free. Next tier: Pro Marketing, $40/month for 5,000 contacts.
- **Transactional (our confirmation emails): 3,000 a month, at most 100 a day.** Next tier: Pro, $20/month for 50,000
  a month with no daily limit. The shop's order emails would share this allowance.
- 3 domains, 1 webhook endpoint, 30-day log retention.
- Registered nonprofits can apply for 50% off paid plans (<https://resend.com/nonprofits>).
- API rate limit: 10 requests per second per team, on every plan (<https://resend.com/docs/api-reference/introduction>).

## What the site does to protect the domain

- Double opt-in: no contact is created until the reader confirms on weelect.org. Confirmation links are signed and
  expire after 48 hours.
- Cross-site form check (Origin header plus a signed form token), a hidden honeypot field, a limit of 5 sign-up
  attempts per 10 minutes per visitor, one confirmation email per address per 10 minutes, and a daily cap on
  confirmation emails.
- Email addresses are never written to our logs. Only "confirmation sent" and "contact confirmed" counts appear in
  Render → Logs.

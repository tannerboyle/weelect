"""Newsletter signup ("Get We Elect updates") backed by Resend (resend.com).

Flow (double opt-in; nothing is stored on our side):
  1. POST /newsletter/subscribe  -> checks origin + signed form token (CSRF), honeypot, rate limits, email format,
                                    then emails a confirmation link via Resend's Send Email API. No contact is created yet.
  2. GET  /newsletter/confirm?t= -> shows a "Confirm my subscription" button. (A GET never subscribes anyone, because
                                    corporate mail scanners open every link in an email.)
  3. POST /newsletter/confirm    -> verifies the signed, expiring token, then creates the contact in Resend and adds it
                                    to the newsletter segment (Resend renamed "Audiences" to "Segments" in Nov 2025).
Unsubscribes are handled by Resend: every broadcast carries {{{RESEND_UNSUBSCRIBE_URL}}} (see docs/NEWSLETTER_SETUP.md).

Env vars (all optional; with any required one missing the site shows "Newsletter coming soon" and never errors):
  RESEND_API_KEY      required. A Full access key (Sending access keys cannot create contacts).
  RESEND_SEGMENT_ID   required. The segment new subscribers join. RESEND_AUDIENCE_ID is accepted as an alias.
  RESEND_TOPIC_ID     optional. If set, subscribers are opted in to this topic.
  NEWSLETTER_FROM     optional. Default "We Elect <updates@news.weelect.org>" (must be on the domain verified in Resend).
  NEWSLETTER_REPLY_TO optional. Reply-to address for the confirmation email.
  NEWSLETTER_POSTAL_ADDRESS  TODO(Bob): Brunner Creative LLC's physical postal address (street address, or a registered
                      USPS PO box or private mailbox), shown in the email footer. CAN-SPAM requires one in every marketing
                      email (each broadcast). Placeholder: unset, so the footer shows no address until it is provided.
  SECRET_KEY          optional but recommended. Signs the tokens; if unset a key is derived from RESEND_API_KEY.
  NEWSLETTER_DAILY_CAP optional. Max confirmation emails per day per worker (default 80; Resend Free sends 100/day total).
"""
import datetime, hashlib, html, os, re, time

import requests
from flask import abort, make_response, redirect, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

API = "https://api.resend.com"
UA = "weelect-newsletter/1.0 (+https://weelect.org)"
CONFIRM_MAX_AGE = 48 * 3600      # confirmation links work for 48 hours
FORM_MAX_AGE = 24 * 3600         # a signup form left open longer than a day must be reloaded
EMAIL_COOLDOWN = 10 * 60         # at most one confirmation email per address per 10 minutes
DEFAULT_FROM = "We Elect <updates@news.weelect.org>"
SENDER_NAME = "Brunner Creative LLC"     # publisher of weelect.org; named in the email footer
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,63}")

_RECENT = {}                     # sha256(email) -> last confirmation send time (memory only, per worker)
_DAY = {"date": None, "count": 0}


class ResendError(Exception):
    pass


# ---------- configuration ----------
def config():
    return {"api_key": os.environ.get("RESEND_API_KEY", "").strip(),
            "segment_id": (os.environ.get("RESEND_SEGMENT_ID") or os.environ.get("RESEND_AUDIENCE_ID") or "").strip(),
            "topic_id": os.environ.get("RESEND_TOPIC_ID", "").strip(),
            "from": os.environ.get("NEWSLETTER_FROM", "").strip() or DEFAULT_FROM,
            "reply_to": os.environ.get("NEWSLETTER_REPLY_TO", "").strip(),
            "postal_address": os.environ.get("NEWSLETTER_POSTAL_ADDRESS", "").strip(),   # TODO(Bob): set in Render
            "daily_cap": int(os.environ.get("NEWSLETTER_DAILY_CAP", "80") or 80)}


def enabled():
    c = config()
    return bool(c["api_key"] and c["segment_id"])


def _serializer(salt):
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:   # derived, never the API key itself; rotating the API key just invalidates open links
        secret = hashlib.sha256(b"weelect-newsletter|" + config()["api_key"].encode()).hexdigest()
    return URLSafeTimedSerializer(secret, salt=salt)


# ---------- tokens ----------
def form_token():
    return _serializer("newsletter-form").dumps("f")


def form_token_ok(tok):
    try:
        return _serializer("newsletter-form").loads(tok or "", max_age=FORM_MAX_AGE) == "f"
    except (BadSignature, SignatureExpired):
        return False


def make_confirm_token(email):
    return _serializer("newsletter-confirm").dumps({"e": email})


def read_confirm_token(tok):
    """Returns (email, None) or (None, "expired" | "invalid")."""
    try:
        data = _serializer("newsletter-confirm").loads(tok or "", max_age=CONFIRM_MAX_AGE)
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "invalid"
    email = data.get("e") if isinstance(data, dict) else None
    return (email, None) if email and valid_email(email) else (None, "invalid")


# ---------- validation ----------
def normalize_email(raw):
    e = (raw or "").strip()
    if "@" in e:
        local, _, domain = e.rpartition("@")
        e = local + "@" + domain.lower()
    return e


def valid_email(e):
    if not e or len(e) > 254 or not EMAIL_RE.fullmatch(e):
        return False
    local = e.rsplit("@", 1)[0]
    return len(local) <= 64 and not local.startswith(".") and not local.endswith(".") and ".." not in e


def same_origin(site_url):
    """CSRF check: the POST must come from a page on this site. Browsers send Origin on form POSTs."""
    allowed = {request.host_url.rstrip("/"), site_url.rstrip("/")}
    origin = request.headers.get("Origin")
    if origin:
        return origin.rstrip("/") in allowed
    ref = request.headers.get("Referer") or ""
    return any(ref == a or ref.startswith(a + "/") for a in allowed)


def _cooldown(email):
    now = time.time()
    if len(_RECENT) > 5000:
        for k in [k for k, t in _RECENT.items() if now - t > EMAIL_COOLDOWN]:
            _RECENT.pop(k, None)
    h = hashlib.sha256(email.lower().encode()).hexdigest()
    if now - _RECENT.get(h, 0) < EMAIL_COOLDOWN:
        return True
    _RECENT[h] = now
    return False


def _over_daily_cap():
    today = datetime.date.today()
    if _DAY["date"] != today:
        _DAY.update(date=today, count=0)
    if _DAY["count"] >= config()["daily_cap"]:
        return True
    _DAY["count"] += 1
    return False


# ---------- Resend REST API ----------
def _resend(method, path, json=None):
    r = requests.request(method, API + path, json=json, timeout=10,
                         headers={"Authorization": "Bearer " + config()["api_key"], "User-Agent": UA,
                                  "Content-Type": "application/json"})
    if r.status_code >= 300:
        try:
            msg = r.json().get("name") or r.json().get("message")
        except ValueError:
            msg = ""
        raise ResendError(f"{method} {path.split('/')[1]} -> {r.status_code} {msg}")
    return r.json() if r.content else {}


def send_confirmation(email, link):
    c = config()
    body = {"from": c["from"], "to": [email], "subject": "Confirm your We Elect updates",
            "html": render_template("email/newsletter_confirm.html", link=link, sender=SENDER_NAME, postal_address=c["postal_address"]),
            "text": render_template("email/newsletter_confirm.txt", link=link, sender=SENDER_NAME, postal_address=c["postal_address"]),
            "tags": [{"name": "kind", "value": "newsletter-confirm"}]}
    if c["reply_to"]:
        body["reply_to"] = c["reply_to"]
    return _resend("POST", "/emails", body)


def add_contact(email):
    """Create the contact (subscribed) in the newsletter segment. If it already exists (for example someone who
    unsubscribed and has now re-confirmed), mark it subscribed and make sure it is in the segment."""
    c = config()
    body = {"email": email, "unsubscribed": False, "segments": [{"id": c["segment_id"]}]}
    if c["topic_id"]:
        body["topics"] = [{"id": c["topic_id"], "subscription": "opt_in"}]
    try:
        return _resend("POST", "/contacts", body)
    except ResendError:
        from urllib.parse import quote
        who = quote(email, safe="@")
        _resend("PATCH", f"/contacts/{who}", {"unsubscribed": False})
        _resend("POST", f"/contacts/{who}/segments/{c['segment_id']}")
        if c["topic_id"]:
            _resend("PATCH", f"/contacts/{who}/topics", [{"id": c["topic_id"], "subscription": "opt_in"}])
        return {"object": "contact", "updated": True}


# ---------- routes ----------
def register(app, rate_limited, private, site_url):
    def page(state, code=200, **kw):
        return private(make_response(render_template("newsletter.html", state=state, **kw), code))

    @app.context_processor
    def _newsletter_globals():
        return {"newsletter_on": enabled(), "newsletter_form_token": form_token if enabled() else (lambda: "")}

    @app.route("/newsletter")
    def newsletter():
        return render_template("newsletter.html", state="signup" if enabled() else "soon")

    @app.route("/newsletter/subscribe", methods=["GET", "POST"])
    def newsletter_subscribe():
        if request.method == "GET":
            return redirect("/newsletter", 303)
        if not enabled():
            return page("soon")
        if not same_origin(site_url) or not form_token_ok(request.form.get("csrf")):
            return page("stale", 400)
        if rate_limited("newsletter", 5, 600):
            return page("slow", 429)
        email = normalize_email(request.form.get("email"))
        if request.form.get("website"):          # honeypot: bots fill it; act as if it worked, send nothing
            return page("sent")
        if not valid_email(email):
            return page("signup", 400, error="Please enter a valid email address, like name@example.com.", email=email[:254])
        if _cooldown(email):                     # same page either way, so nobody can probe who is subscribed
            return page("sent")
        if _over_daily_cap():
            print("NEWSLETTER daily confirmation cap reached", flush=True)
            return page("busy", 503)
        link = site_url.rstrip("/") + "/newsletter/confirm?t=" + make_confirm_token(email)
        try:
            send_confirmation(email, link)
        except (ResendError, requests.RequestException) as e:
            print(f"NEWSLETTER confirm email failed: {e}", flush=True)   # never logs the address
            return page("error", 502)
        print("NEWSLETTER confirmation sent", flush=True)
        return page("sent")

    @app.route("/newsletter/confirm", methods=["GET", "POST"])
    def newsletter_confirm():
        if not enabled():
            return page("soon")
        tok = request.values.get("t", "")
        email, problem = read_confirm_token(tok)
        if problem:
            return page(problem, 400)
        if request.method == "GET":
            return page("confirm", token=tok, email=email)
        if not same_origin(site_url):
            return page("stale", 400)
        if rate_limited("newsletter-confirm", 20, 60):
            return page("slow", 429)
        try:
            add_contact(email)
        except (ResendError, requests.RequestException) as e:
            print(f"NEWSLETTER add contact failed: {e}", flush=True)
            return page("error", 502)
        print("NEWSLETTER contact confirmed", flush=True)
        return page("confirmed")

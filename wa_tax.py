"""Washington destination-based sales tax for the We Elect shop.

Looks up the combined state + local retail sales tax rate for a Washington delivery address with the WA Department of
Revenue's free address rate lookup ("URL interface", https://dor.wa.gov/wa-sales-tax-rate-lookup-url-interface):

    GET https://webgis.dor.wa.gov/webapi/AddressRates.aspx?output=xml&addr=<street>&city=<city>&zip=<5 or 9 digit ZIP>

The XML answer looks like (checked 2026-11-26):

    <response loccode="1800" localrate=".027" rate=".092" code="0"><addressline ... period="Q42026" .../>
      <rate name="KITSAP PTBA" code="1800" staterate=".065" localrate=".027" /></response>

Result codes (DOR): 0 address found; 1 address not found but ZIP+4 found; 2/3/4 address updated or corrected and found;
5 only the 5-digit ZIP found (rate for the ZIP's main area); 6 nothing found; 7 bad lat/long; 9 internal error
(also used for "Zipcode is invalid for WA State"). Codes 0-5 give a usable rate; 6 and an invalid-ZIP 9 mean the
address is not in Washington as typed; anything else is treated as a failed lookup (the shop then uses its fallback).

Answers are cached in memory (rates change at most quarterly). Failed lookups are remembered for a minute so an outage
at DOR does not slow every checkout.
"""
import re, threading, time, logging
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
import requests

log = logging.getLogger("weelect.wa_tax")

DOR_URL = "https://webgis.dor.wa.gov/webapi/AddressRates.aspx"
TIMEOUT = 6                  # seconds; checkout falls back rather than hang
CACHE_TTL = 12 * 3600
FAIL_TTL = 60
MAX_BYTES = 64 * 1024
WA_STATE_RATE = Decimal("0.065")
MAX_RATE = Decimal("0.15")   # no WA location is anywhere near this; anything above is treated as a bad answer
USABLE = {0, 1, 2, 3, 4, 5}

_CACHE = {}
_LOCK = threading.Lock()


class LookupFailed(Exception):
    """DOR could not be asked or gave no usable answer (network, timeout, HTTP error, bad XML, internal error)."""


class AddressNotFound(Exception):
    """DOR answered that this street/ZIP is not a Washington address it knows."""


def reset():
    with _LOCK:
        _CACHE.clear()


def _key(line1, city, zip_code):
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().upper())
    return (norm(line1), norm(city), re.sub(r"[^0-9]", "", zip_code or ""))


def fetch(line1, city, zip_code):
    """One HTTP call to DOR. Returns the raw XML text or raises LookupFailed."""
    params = {"output": "xml", "addr": line1 or "", "city": city or "", "zip": re.sub(r"[^0-9]", "", zip_code or "")}
    try:
        r = requests.get(DOR_URL, params=params, timeout=TIMEOUT, headers={"Accept": "text/xml"})
    except requests.RequestException as e:
        raise LookupFailed(f"network {type(e).__name__}")
    if r.status_code != 200:
        raise LookupFailed(f"http {r.status_code}")
    text = r.text or ""
    if len(text) > MAX_BYTES:
        raise LookupFailed("response too large")
    return text


def parse(text):
    """DOR XML -> {"rate": Decimal fraction, "loccode": "1800", "code": 0, "period": "Q42026", "name": "KITSAP PTBA"}."""
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise LookupFailed("unexpected DTD in response")
    try:
        root = ET.fromstring(text.strip().encode("utf-8"))
    except ET.ParseError:
        raise LookupFailed("unreadable XML")
    if root.tag != "response":
        raise LookupFailed("unexpected XML")
    try:
        code = int(root.get("code", ""))
    except ValueError:
        raise LookupFailed("no result code")
    hint = (root.get("debughint") or "").lower()
    if code == 6 or (code == 9 and "invalid" in hint and "zip" in hint):
        raise AddressNotFound(f"code {code} {hint}".strip())
    if code not in USABLE:
        raise LookupFailed(f"result code {code} {hint}".strip())
    try:
        rate = Decimal(root.get("rate", ""))
    except InvalidOperation:
        raise LookupFailed("no rate")
    loccode = (root.get("loccode") or "").strip()
    if not (WA_STATE_RATE <= rate <= MAX_RATE) or not re.fullmatch(r"\d{4}", loccode):
        raise LookupFailed(f"implausible answer rate={rate} loccode={loccode!r}")
    line = root.find("addressline")
    rate_el = root.find("rate")
    return {"rate": rate, "loccode": loccode, "code": code,
            "period": (line.get("period") if line is not None else None) or None,
            "name": (rate_el.get("name") if rate_el is not None else None) or None}


def lookup(line1, city, zip_code):
    """Cached DOR lookup. Returns the parsed dict, or raises LookupFailed / AddressNotFound."""
    key = _key(line1, city, zip_code)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[1] < (CACHE_TTL if hit[0][0] == "ok" else FAIL_TTL):
        kind, val = hit[0]
        if kind == "ok":
            return dict(val)
        raise (AddressNotFound if kind == "notfound" else LookupFailed)(val)
    try:
        res = parse(fetch(line1, city, zip_code))
        entry = ("ok", res)
    except AddressNotFound as e:
        entry = ("notfound", str(e))
    except LookupFailed as e:
        log.warning("WA DOR rate lookup failed: %s", e)
        entry = ("failed", str(e))
    with _LOCK:
        if len(_CACHE) > 2000:
            _CACHE.clear()
        _CACHE[key] = (entry, now)
    kind, val = entry
    if kind == "ok":
        return dict(val)
    raise (AddressNotFound if kind == "notfound" else LookupFailed)(val)

#!/usr/bin/env python3
"""
fetch_prices.py — update prices.json from eBay Browse API.

Reads the per-item config already stored in prices.json (query, excludeTerms,
conditionFactor), fetches active listings, and appends one price reading per item.

Usage:
    python3 scripts/fetch_prices.py                  # live, needs EBAY_CLIENT_ID/SECRET
    python3 scripts/fetch_prices.py --mock           # use fixtures, no network
    python3 scripts/fetch_prices.py --dry-run        # fetch but don't write the file

Design rules (see ARCHITECTURE.md):
  * median, never mean  — novelty listings destroy averages
  * trim outliers first — drop top/bottom 15% when there's enough data
  * exclude parts       — "A1502" matches batteries and logic boards
  * carry forward       — thin markets return nothing some days; never write 0
  * never write null/0  — the site treats those as "no reading"
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRICES_PATH = REPO_ROOT / "prices.json"
FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures.json"

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"

RESULTS_PER_ITEM = 50      # eBay Browse max is 200; 50 is plenty and stays polite
TRIM_FRACTION = 0.15       # drop this much from each end before taking the median
MIN_FOR_TRIM = 4           # below this many samples, trimming throws away too much
REQUEST_PAUSE = 0.5        # seconds between item searches
SANITY_FACTOR = 2.5        # warn (don't block) if price moves more than this multiple


# ----------------------------------------------------------------------------
# pure logic — all of this is unit-tested and touches no network
# ----------------------------------------------------------------------------

def title_is_excluded(title, exclude_terms):
    """True if the listing title contains any kill-word. Case-insensitive."""
    if not title:
        return True
    low = title.lower()
    return any(term.lower() in low for term in exclude_terms)


def extract_listings(payload):
    """
    Pull (item_id, title, price) out of an eBay Browse response.
    Skips anything without a usable USD price.
    """
    out = []
    for summary in (payload or {}).get("itemSummaries") or []:
        price = summary.get("price") or {}
        currency = price.get("currency")
        raw = price.get("value")
        if currency != "USD" or raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        out.append({
            "id": summary.get("itemId") or "",
            "title": summary.get("title") or "",
            "price": value,
        })
    return out


def dedupe(listings):
    """eBay can repeat an item across pages. Keep first occurrence of each id."""
    seen = set()
    out = []
    for entry in listings:
        key = entry["id"]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(entry)
    return out


def filter_listings(listings, exclude_terms):
    return [e for e in listings if not title_is_excluded(e["title"], exclude_terms)]


def median(values):
    """Median of a non-empty list. Even count averages the two middle values."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def trimmed_median(values, trim_fraction=TRIM_FRACTION, min_for_trim=MIN_FOR_TRIM):
    """
    Median after dropping the extremes. This is the whole defence against a
    sealed first-gen iPhone listed at $19,500 sitting in the same result set
    as a $40 parts unit.
    """
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n < min_for_trim:
        return median(s)
    k = int(n * trim_fraction)
    if k == 0:
        return median(s)
    if n - 2 * k <= 0:          # trimming would empty the list
        return median(s)
    return median(s[k:n - k])


def apply_condition(price, factor):
    """Map a market median onto this specific unit's condition."""
    if price is None:
        return None
    try:
        f = float(factor)
    except (TypeError, ValueError):
        f = 1.0
    if f <= 0:
        f = 1.0
    return round(price * f, 2)


def valid_history(item):
    """Existing readings that the site would consider real."""
    out = []
    for pt in item.get("history") or []:
        p = pt.get("price")
        if isinstance(p, (int, float)) and p > 0:
            out.append(pt)
    return out


def previous_price(item):
    hist = valid_history(item)
    return hist[-1]["price"] if hist else None


def build_entry(price, samples, source, date_str):
    return {
        "date": date_str,
        "price": round(float(price), 2),
        "samples": int(samples),
        "source": source,
    }


def upsert_history(item, entry):
    """
    Append the reading — unless one already exists for the same date, in which
    case replace it. Keeps a same-day re-run from creating two points and
    inventing a fake intraday move.
    """
    history = item.setdefault("history", [])
    if history and history[-1].get("date") == entry["date"]:
        history[-1] = entry
        return "replaced"
    history.append(entry)
    return "appended"


def price_for_item(listings, item):
    """
    Full pipeline for one item's raw listings.
    Returns (price, sample_count, source) — price is None if nothing usable.
    """
    clean = filter_listings(dedupe(listings), item.get("excludeTerms") or [])
    values = [e["price"] for e in clean]
    if not values:
        return None, 0, None
    raw_median = trimmed_median(values)
    if raw_median is None or raw_median <= 0:
        return None, 0, None
    final = apply_condition(raw_median, item.get("conditionFactor", 1.0))
    if final is None or final <= 0:
        return None, 0, None
    return final, len(values), "ebay-browse"


# ----------------------------------------------------------------------------
# network
# ----------------------------------------------------------------------------

def http_json(req, timeout=30):
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def get_token(client_id, client_secret):
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": EBAY_SCOPE,
    }).encode()
    req = urllib.request.Request(
        EBAY_OAUTH_URL,
        data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    payload = http_json(req)
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("no access_token in eBay OAuth response")
    return token


def search(token, query, limit=RESULTS_PER_ITEM):
    params = urllib.parse.urlencode({
        "q": query,
        "limit": str(limit),
        # Fixed-price only. A live auction's current bid is not a market price —
        # it's whatever the bidding happens to be at right now.
        "filter": "buyingOptions:{FIXED_PRICE}",
    })
    req = urllib.request.Request(
        f"{EBAY_SEARCH_URL}?{params}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "Accept": "application/json",
        },
    )
    return http_json(req)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run(mock=False, dry_run=False, verbose=True):
    data = load_json(PRICES_PATH)
    items = data.get("items") or []
    if not items:
        print("no items in prices.json", file=sys.stderr)
        return 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fixtures = {}
    token = None
    if mock:
        if not FIXTURES_PATH.exists():
            print(f"missing fixtures at {FIXTURES_PATH}", file=sys.stderr)
            return 1
        fixtures = load_json(FIXTURES_PATH)
    else:
        cid = os.environ.get("EBAY_CLIENT_ID")
        secret = os.environ.get("EBAY_CLIENT_SECRET")
        if not cid or not secret:
            print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set", file=sys.stderr)
            return 1
        token = get_token(cid, secret)

    fetched = carried = failed = 0

    for item in items:
        sym = item.get("symbol", item.get("id", "?"))
        prev = previous_price(item)

        try:
            if mock:
                payload = fixtures.get(item["id"], {})
            else:
                payload = search(token, item["query"])
                time.sleep(REQUEST_PAUSE)
            listings = extract_listings(payload)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            listings = []
            if verbose:
                print(f"  {sym}: request failed ({exc})")

        price, samples, source = price_for_item(listings, item)

        if price is None:
            # Thin market or a failed request. Repeat the last good price rather
            # than writing a zero, which the site would render as a -100% crash.
            if prev is None:
                failed += 1
                if verbose:
                    print(f"  {sym}: no data and no prior price — skipped")
                continue
            entry = build_entry(prev, 0, "carry-forward", today)
            carried += 1
            if verbose:
                print(f"  {sym}: no usable listings — carried forward ${prev:.2f}")
        else:
            entry = build_entry(price, samples, source, today)
            fetched += 1
            if verbose:
                delta = ""
                if prev:
                    pct = (price - prev) / prev * 100
                    delta = f"  ({pct:+.1f}% vs ${prev:.2f})"
                    if prev > 0 and (price > prev * SANITY_FACTOR or price < prev / SANITY_FACTOR):
                        delta += "  <-- LARGE MOVE, worth eyeballing"
                print(f"  {sym}: ${price:.2f} from {samples} listings{delta}")

        upsert_history(item, entry)

    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    data["source"] = "mock" if mock else "ebay-browse"

    print(f"\n{fetched} fetched, {carried} carried forward, {failed} skipped")

    if dry_run:
        print("dry run — prices.json not written")
        return 0

    with open(PRICES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {PRICES_PATH}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="use fixtures instead of the network")
    ap.add_argument("--dry-run", action="store_true", help="don't write prices.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    return run(mock=args.mock, dry_run=args.dry_run, verbose=not args.quiet)


if __name__ == "__main__":
    sys.exit(main())

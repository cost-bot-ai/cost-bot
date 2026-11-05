#!/usr/bin/env python3
"""Phase 0 spike: how much US retail price data does Open Prices (Open Food Facts) actually hold?

Read-only, ~25 paced requests, identified User-Agent. Stops on the first 403/429/5xx, never retries.
Open Prices is volunteer-run: keep this small. Output: JSON summary on stdout; raw responses not stored.

Method (and why): per-retailer coverage is computed from the US *location* records (each carries the
store's OSM name and its price_count), matched locally. An earlier version filtered prices server-side
with `location__osm_name__contains` and `currency=USD`; both were unreliable (the name filter returned 0 for
a name known to exist; USD includes non-US stores). The script now SELF-CHECKS every server-side filter
it relies on against known data and says so in the output instead of reporting a silent zero.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

import httpx

BASE = "https://prices.openfoodfacts.org/api/v1"
UA = "PriceMemory-spike/0.1 (price research; +https://github.com/cost-bot-ai/cost-bot)"
DELAY = 2.0
MAX_REQUESTS = 45
MAX_IDS = 200  # location ids per location_id__in request

# retailer -> lowercase substrings matched against the store's OSM name
RETAILERS = {
    "Kroger": ["kroger"], "Ralphs": ["ralphs"], "King Soopers": ["king soopers"],
    "Harris Teeter": ["harris teeter"], "Fred Meyer": ["fred meyer"],
    "Walmart": ["walmart"], "Target": ["target"], "Aldi": ["aldi"],
    "Albertsons": ["albertsons"], "Safeway": ["safeway"],
    "H-E-B": ["h-e-b", "heb "], "Whole Foods": ["whole foods"],
}


class Stop(Exception):
    """Raised on the first sign we should not continue (block, rate limit, server error, budget)."""


class Client:
    def __init__(self, http: httpx.Client, delay: float = DELAY, budget: int = MAX_REQUESTS):
        self.http, self.delay, self.budget, self.used = http, delay, budget, 0

    def get(self, path: str, **params) -> dict | list:
        if self.used >= self.budget:
            raise Stop(f"request budget of {self.budget} reached")
        if self.used:
            time.sleep(self.delay)
        self.used += 1
        r = self.http.get(f"{BASE}{path}", params=params)
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            raise Stop(f"HTTP {r.status_code} on {path}; stopped, no retry")
        r.raise_for_status()
        return r.json()


def us_locations(c: Client) -> list[dict]:
    items, page = [], 1
    while True:
        r = c.get("/locations", osm_address_country__like="United States", size=100, page=page)
        items += [{"id": x["id"], "name": x.get("osm_name") or "", "price_count": x.get("price_count") or 0}
                  for x in r["items"]]
        if page >= r["pages"]:
            return items
        page += 1


def matches(name: str, variants: list[str]) -> bool:
    n = (name + " ").lower()
    return any(v in n for v in variants)


def total_for_ids(c: Client, ids: list[int], **extra) -> int:
    return int(c.get("/prices", size=1, location_id__in=",".join(map(str, ids[:MAX_IDS])), **extra)["total"])


def collect(c: Client, anchors: list[str], since: str) -> dict:
    out: dict = {"since": since, "checks": {}}
    stats = c.get("/stats")
    out["overall"] = {k: stats.get(k) for k in (
        "price_count", "product_with_price_count", "price_location_country_count",
        "price_source_web_count", "price_source_mobile_count", "price_source_api_count")}

    countries = c.get("/locations/osm/countries")
    top = sorted(countries, key=lambda x: x.get("price_count", 0), reverse=True)[:8]
    out["top_countries"] = [(x["country_code_2"], x["name"], x["location_count"], x["price_count"]) for x in top]
    us = next((x for x in countries if x.get("country_code_2") == "US"), None)
    out["us"] = {"locations": us and us["location_count"], "prices": us and us["price_count"]}

    locs = us_locations(c)
    loc_prices = sum(x["price_count"] for x in locs)
    out["us_locations_fetched"] = len(locs)
    # SELF-CHECK 1: the location records must roughly add up to the country total the API reports.
    out["checks"]["location_price_counts_sum_matches_country_total"] = bool(
        us and us["price_count"] and abs(loc_prices - us["price_count"]) <= 0.05 * us["price_count"])
    out["checks"]["us_location_count_matches_country_row"] = bool(us and len(locs) == us["location_count"])

    by_name: dict[str, int] = {}
    for x in locs:
        by_name[x["name"]] = by_name.get(x["name"], 0) + x["price_count"]
    out["top_us_store_names"] = sorted(by_name.items(), key=lambda kv: -kv[1])[:15]

    all_ids = [x["id"] for x in locs]
    out["us_prices_recent"] = total_for_ids(c, all_ids, created__gte=since) if all_ids else None
    # SELF-CHECK 2: the date filter must not return more than the unfiltered total.
    out["checks"]["date_filter_is_a_subset"] = (
        out["us_prices_recent"] is not None and us and out["us_prices_recent"] <= us["price_count"] + 0)

    out["retailers"] = {}
    for name, variants in RETAILERS.items():
        mine = [x for x in locs if matches(x["name"], variants)]
        rec = {"locations": len(mine), "prices_all_time": sum(x["price_count"] for x in mine)}
        if mine:
            rec["prices_recent"] = total_for_ids(c, [x["id"] for x in mine], created__gte=since)
        out["retailers"][name] = rec

    # SELF-CHECK 3: product_code__in must find a code that is known to exist.
    probe = c.get("/prices", size=1, location_id__in=",".join(map(str, all_ids[:MAX_IDS]))) if all_ids else {"items": []}
    known = probe["items"][0]["product"]["code"] if probe["items"] else None
    found_known = c.get("/prices", size=1, product_code__in=known)["total"] if known else 0
    out["checks"]["product_code_in_finds_a_known_code"] = bool(known and found_known >= 1)

    codes = sorted(set(anchors + [a.lstrip("0") for a in anchors] + [a[1:] for a in anchors if a.startswith("0")]))
    page = c.get("/prices", product_code__in=",".join(codes), size=50) if codes else {"total": 0, "items": []}
    out["anchor_upcs"] = {
        "queried": len(anchors), "total_prices_found": page["total"],
        "trustworthy": out["checks"]["product_code_in_finds_a_known_code"],
        "sample": [{"code": i["product"]["code"], "price": i["price"], "currency": i["currency"], "date": i["date"],
                    "store": (i.get("location") or {}).get("osm_name")} for i in page["items"][:10]]}
    out["all_checks_passed"] = all(bool(v) for v in out["checks"].values())
    out["requests_used"] = c.used
    return out


def main() -> int:
    anchors = []
    p = pathlib.Path("raw/spike_kroger/cross_metro.json")
    if p.exists():
        anchors = json.loads(p.read_text())["anchors"]
    since = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    with httpx.Client(timeout=40, headers={"User-Agent": UA, "Accept": "application/json"}) as http:
        c = Client(http)
        try:
            result = collect(c, anchors, since)
        except Stop as e:
            print(json.dumps({"stopped": str(e), "requests_used": c.used}, indent=2))
            return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

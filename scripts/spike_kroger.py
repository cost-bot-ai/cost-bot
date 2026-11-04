#!/usr/bin/env python3
"""Phase 0 spike: learn what the Kroger public API actually does, instead of assuming.

Needs: KROGER_CLIENT_ID, KROGER_CLIENT_SECRET in the environment, and network access to
api.kroger.com. Prints (and saves under raw/spike_kroger/) the raw response shapes plus
rate-limit headers so the plan's call budget can be set from facts.

Questions it answers (each printed as a labelled section):
  1. Token endpoint works; token lifetime.
  2. Locations: does filter.zipCode.near + radius work; what fields come back (id, banner...).
  3. Products: does filter.term + filter.locationId return a price; where is it in the payload.
  4. Does filter.productId accept several comma-separated IDs in ONE call (batching)?
  5. Which rate-limit headers come back, if any.
  6. Price differences for the same term across the metros in data/curated/kroger_panel.yaml.

Usage:  python scripts/spike_kroger.py --term "large eggs" --metros cincinnati,los-angeles,denver
Stops immediately on 401/403/429 and says so. Makes < ~60 requests by default.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import time

import httpx
import yaml

BASE = "https://api.kroger.com/v1"
OUT = pathlib.Path("raw/spike_kroger")
UA = "PriceMemory-spike/0.1 (research; contact via repo)"


def save(name: str, payload) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str))


def rate_headers(resp: httpx.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if "rate" in k.lower() or "limit" in k.lower() or "retry" in k.lower()}


def stop_if_blocked(resp: httpx.Response, what: str) -> None:
    if resp.status_code in (401, 403, 429):
        print(f"\nSTOP: {what} returned HTTP {resp.status_code}. Headers: {rate_headers(resp)}")
        print(resp.text[:500])
        sys.exit(2)


def get_token(client: httpx.Client, cid: str, secret: str) -> str:
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = client.post(
        f"{BASE}/connect/oauth2/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "product.compact"},
    )
    stop_if_blocked(r, "token")
    r.raise_for_status()
    body = r.json()
    print(f"[1] token ok; expires_in={body.get('expires_in')} scope requested=product.compact")
    return body["access_token"]


def find_price(product: dict):
    """Locate a price in a product payload without assuming one exact shape."""
    for item in product.get("items", []) or []:
        if "price" in item:
            return item["price"]
    return product.get("price")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--term", default="large eggs")
    ap.add_argument("--metros", default="cincinnati,los-angeles,denver")
    ap.add_argument("--panel", default="data/curated/kroger_panel.yaml")
    args = ap.parse_args()

    cid, secret = os.environ.get("KROGER_CLIENT_ID"), os.environ.get("KROGER_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET (never commit them).")

    panel = yaml.safe_load(pathlib.Path(args.panel).read_text())
    wanted = {s.strip() for s in args.metros.split(",")}
    metros = [m for m in panel["metros"] if m["slug"] in wanted]

    with httpx.Client(timeout=30, headers={"User-Agent": UA}) as client:
        token = get_token(client, cid, secret)
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        results = {}
        requests_made = 1

        for metro in metros:
            time.sleep(2)  # polite pacing
            r = client.get(f"{BASE}/locations", headers=auth, params={
                "filter.zipCode.near": metro["zip"], "filter.radiusInMiles": 10, "filter.limit": 5})
            requests_made += 1
            stop_if_blocked(r, f"locations {metro['slug']}")
            r.raise_for_status()
            locs = r.json().get("data", [])
            save(f"locations_{metro['slug']}", r.json())
            print(f"[2] {metro['slug']}: {len(locs)} locations; fields on first: {sorted(locs[0].keys()) if locs else '-'}; "
                  f"banners: {sorted({l.get('chain') for l in locs})}; rate headers: {rate_headers(r)}")
            if not locs:
                continue
            loc_id = locs[0]["locationId"]

            time.sleep(2)
            r = client.get(f"{BASE}/products", headers=auth, params={
                "filter.term": args.term, "filter.locationId": loc_id, "filter.limit": 10})
            requests_made += 1
            stop_if_blocked(r, f"products {metro['slug']}")
            r.raise_for_status()
            prods = r.json().get("data", [])
            save(f"products_{metro['slug']}", r.json())
            print(f"[3] {metro['slug']} loc={loc_id}: {len(prods)} products; rate headers: {rate_headers(r)}")
            results[metro["slug"]] = {
                p.get("upc") or p.get("productId"): find_price(p) for p in prods}

        # 4. batching test with the first metro's first few product ids
        first = next(iter(results.values()), {})
        ids = [i for i in first if i][:3]
        if ids and metros:
            time.sleep(2)
            r = client.get(f"{BASE}/products", headers=auth, params={
                "filter.productId": ",".join(ids), "filter.locationId": loc_id})
            requests_made += 1
            stop_if_blocked(r, "batch products")
            got = len(r.json().get("data", [])) if r.status_code == 200 else None
            print(f"[4] batch of {len(ids)} ids in one call -> HTTP {r.status_code}, returned {got} products "
                  f"(batching {'WORKS' if got and got > 1 else 'NOT confirmed'})")

        # 6. cross-metro comparison for UPCs seen in every metro
        common = set.intersection(*[set(v) for v in results.values()]) if results else set()
        print(f"[6] UPCs present in all {len(results)} metros: {len(common)}")
        for upc in sorted(common)[:10]:
            print("   ", upc, {m: results[m][upc] for m in results})
        save("cross_metro", {"results": results})
        print(f"\nDone. {requests_made} requests made. Raw payloads in {OUT}/")


if __name__ == "__main__":
    main()

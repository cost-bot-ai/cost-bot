#!/usr/bin/env python3
"""Phase 0 spike: can a logged-out, identified, polite client read a product page's price at Target / Walmart?

Rules this script enforces (they are the point, not decoration):
  * Product PAGES only. Never search pages, never undocumented API endpoints.
  * robots.txt is fetched and honored in code for every URL before it is requested.
  * Identified User-Agent with a contact URL. No header, TLS, cookie or fingerprint tricks, no proxies.
  * >= 5 s between requests, max 2 product pages per retailer.
  * On the FIRST non-200 status or challenge page for a retailer it stops that retailer. No retry.
  * Raw HTML is saved only under raw/ (git-ignored) for local inspection; it is never committed.

Usage:  python scripts/spike_retailers.py            # both retailers, built-in URLs
        python scripts/spike_retailers.py --only target
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

UA = "PriceMemory-spike/0.1 (price research; +https://github.com/cost-bot-ai/cost-bot)"
OUT = pathlib.Path("raw/spike_retailers")
DELAY = 5.0
MAX_PAGES = 2

TARGETS = {
    "target": [
        "https://www.target.com/p/grade-a-large-eggs-12ct-good-38-gather-8482-packaging-may-vary/-/A-14713534",
        "https://www.target.com/p/cage-free-large-white-fresh-grade-a-eggs-ca-sefs-compliant-24oz-12ct-good-38-gather-8482-packaging-may-vary/-/A-83719456",
    ],
    "walmart": [
        "https://www.walmart.com/ip/Great-Value-Cage-Free-Large-White-Eggs-12-Count/421705528",
    ],
}

# Text that marks a bot-wall / block page. Matching ANY of these on a response means: stop.
CHALLENGE_MARKERS = [
    "robot or human", "px-captcha", "_pxcaptcha", "access denied", "pardon our interruption",
    "request blocked", "are you a human", "unusual traffic", "verify you are human", "captcha",
]
BLOCK_STATUSES = {401, 403, 405, 406, 407, 412, 418, 429, 451, 503}


def robots_for(client: httpx.Client, host: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    r = client.get(f"https://{host}/robots.txt")
    rp.parse(r.text.splitlines() if r.status_code == 200 else ["User-agent: *", "Disallow: /"])
    return rp


def classify(resp: httpx.Response) -> tuple[str, str]:
    """Return (verdict, reason). verdict in ok | blocked | challenge."""
    body = resp.text.lower()
    if resp.status_code in BLOCK_STATUSES:
        return "blocked", f"HTTP {resp.status_code}"
    if resp.status_code != 200:
        return "blocked", f"HTTP {resp.status_code} (unexpected)"
    hits = [m for m in CHALLENGE_MARKERS if m in body]
    has_product = ('"@type":"product"' in body.replace(" ", "")) or "__next_data__" in body
    if hits and not has_product:
        return "challenge", f"200 but challenge markers {hits[:3]} and no product data"
    return "ok", "200 with product-like content" if has_product else "200 but no recognised product data"


def jsonld_products(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get("@type") in ("Product", ["Product"]):
                out.append(it)
    return out


def find_item_price(html: str, item_id: str) -> dict:
    """Find the price of THIS page's item (not an ad slot's) in the embedded __NEXT_DATA__ JSON.

    Looks for a node whose usItemId/tcin equals `item_id` and that carries priceInfo.currentPrice.price.
    Returns {"price_found": bool, "price": float|None, "unit_price": str|None, "name": str|None}.
    """
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    empty = {"price_found": False, "price": None, "unit_price": None, "name": None}
    if not m:
        return empty
    try:
        data = json.loads(m.group(1))
    except Exception:
        return empty
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if str(node.get("usItemId") or node.get("tcin")) == str(item_id):
                pi = node.get("priceInfo") or {}
                cur = (pi.get("currentPrice") or {}).get("price")
                if cur is not None:
                    return {"price_found": True, "price": cur,
                            "unit_price": (pi.get("unitPrice") or {}).get("priceString"), "name": node.get("name")}
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return empty


def item_id_from_url(url: str) -> str:
    m = re.search(r"(?:/A-|/)(\d{6,})(?:[/?]|$)", urlparse(url).path)
    return m.group(1) if m else ""


def summarize_offers(prod: dict) -> dict:
    offers = prod.get("offers")
    offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    return {
        "name": prod.get("name"), "gtin": prod.get("gtin13") or prod.get("gtin") or prod.get("gtin12"),
        "sku": prod.get("sku"),
        "offers": [{k: o.get(k) for k in ("price", "lowPrice", "highPrice", "priceCurrency", "availability")} for o in offers if isinstance(o, dict)],
    }


def run(name: str, urls: list[str], client: httpx.Client) -> dict:
    result = {"retailer": name, "pages": [], "stopped": None}
    host = urlparse(urls[0]).netloc
    rp = robots_for(client, host)
    time.sleep(DELAY)
    for i, url in enumerate(urls[:MAX_PAGES]):
        path = urlparse(url).path
        if not rp.can_fetch(UA, url):
            result["pages"].append({"url": url, "robots": "DISALLOWED", "fetched": False})
            result["stopped"] = f"robots.txt disallows {path}"
            return result
        resp = client.get(url)
        verdict, reason = classify(resp)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}_{i}.html").write_text(resp.text)
        page = {"url": url, "robots": "allowed", "status": resp.status_code, "bytes": len(resp.content),
                "verdict": verdict, "reason": reason, "final_url": str(resp.url),
                "rate_headers": {k: v for k, v in resp.headers.items() if "rate" in k.lower() or "retry" in k.lower()}}
        if verdict == "ok":
            prods = jsonld_products(resp.text)
            page["jsonld_products"] = [summarize_offers(p) for p in prods]
            page["has_next_data"] = "__NEXT_DATA__" in resp.text
            page.update(find_item_price(resp.text, item_id_from_url(url)))
            m = re.search(r'"(?:store|storeId|store_id|pricing_store_id)"\s*:\s*"?(\w+)"?', resp.text)
            page["store_hint"] = m.group(1) if m else None
        result["pages"].append(page)
        if verdict != "ok":
            result["stopped"] = f"{verdict}: {reason}. Stopped; no retry."
            return result
        time.sleep(DELAY)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(TARGETS))
    args = ap.parse_args()
    names = [args.only] if args.only else list(TARGETS)
    with httpx.Client(timeout=30, headers={"User-Agent": UA, "Accept": "text/html"}, follow_redirects=True,
                      max_redirects=3) as client:
        results = [run(n, TARGETS[n], client) for n in names]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    sys.exit(main())

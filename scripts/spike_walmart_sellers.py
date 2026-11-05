#!/usr/bin/env python3
"""Spike: how much of Walmart's published catalog is sold by Walmart itself (first party)?

The sitemaps list product URLs but not who sells them; the product page does (`sellerType`).
This draws a small, SEEDED random sample from sitemap files you already downloaded, reads each page with
the same polite client rules as scripts/spike_retailers.py (robots.txt enforced, identified User-Agent, >= 5 s
apart, stop on the first block or challenge, no retry), and reports first-party vs marketplace.

It stores only the extracted seller fields, never page HTML.

Usage:
  python scripts/spike_walmart_sellers.py --sitemaps /path/child1.xml.gz /path/child2.xml.gz --n 20 --seed 7
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import pathlib
import random
import re
import sys
import time
from urllib.parse import urlparse

import httpx

_here = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("spike_retailers", _here / "spike_retailers.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)

UA, DELAY = sr.UA, sr.DELAY
MAX_PAGES = 25  # hard ceiling for this spike regardless of --n

# Walmart-owned / Walmart-exclusive brands (slug prefixes). A slug heuristic only: it finds private
# label, not every item Walmart itself sells (national brands sold by Walmart are not caught).
PRIVATE_LABEL_PREFIXES = [
    "great-value", "equate", "marketside", "spring-valley", "parent-s-choice", "sam-s-choice",
    "mainstays", "better-homes-gardens", "the-pioneer-woman", "time-and-tru", "wonder-nation",
    "athletic-works", "no-boundaries", "ozark-trail", "hyper-tough", "onn", "vizio",
]


def urls_from_sitemaps(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        raw = pathlib.Path(p).read_bytes()
        try:
            text = gzip.decompress(raw).decode("utf-8", "replace")
        except OSError:
            text = raw.decode("utf-8", "replace")
        out += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text) if "/ip/" in u]
    return out


def slug(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[1].lower() if len(parts) >= 2 and parts[0] == "ip" else ""


def is_private_label(url: str) -> bool:
    s = slug(url)
    return any(s == p or s.startswith(p + "-") for p in PRIVATE_LABEL_PREFIXES)


def draw_sample(urls: list[str], n: int, seed: int) -> list[str]:
    return random.Random(seed).sample(urls, min(n, len(urls)))


def seller_info(html: str) -> dict:
    """Seller fields from the embedded page data. Missing data yields sellerType None (never a guess)."""
    empty = {"sellerType": None, "sellerDisplayName": None, "offerType": None, "price": None, "name": None}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return empty
    try:
        d = json.loads(m.group(1))
        prod = d["props"]["pageProps"]["initialData"]["data"]["product"]
    except Exception:
        return empty
    if not isinstance(prod, dict):
        return empty
    price = ((prod.get("priceInfo") or {}).get("currentPrice") or {}).get("price")
    return {"sellerType": prod.get("sellerType"), "sellerDisplayName": prod.get("sellerDisplayName"),
            "offerType": prod.get("offerType"), "price": price, "name": prod.get("name")}


def classify_seller(info: dict) -> str:
    t = info.get("sellerType")
    return "first_party" if t == "INTERNAL" else "marketplace" if t == "EXTERNAL" else "unknown"


def run(urls: list[str], client: httpx.Client, budget: int = MAX_PAGES) -> dict:
    result = {"pages": [], "stopped": None}
    rp = sr.robots_for(client, urlparse(urls[0]).netloc)
    time.sleep(DELAY)
    for url in urls[:min(budget, MAX_PAGES)]:
        if not rp.can_fetch(UA, url):
            result["pages"].append({"url": url, "robots": "DISALLOWED"})
            result["stopped"] = f"robots.txt disallows {urlparse(url).path[:60]}"
            return result
        resp = client.get(url)
        verdict, reason = sr.classify(resp)
        page = {"url": url, "status": resp.status_code, "verdict": verdict}
        if verdict != "ok":
            result["pages"].append(page)
            result["stopped"] = f"{verdict}: {reason}. Stopped; no retry."
            return result
        info = seller_info(resp.text)
        page.update(info)
        page["class"] = classify_seller(info)
        page["private_label_slug"] = is_private_label(url)
        result["pages"].append(page)
        time.sleep(DELAY)
    return result


def summarize(result: dict) -> dict:
    pages = [p for p in result["pages"] if "class" in p]
    n = len(pages)
    counts = {k: sum(1 for p in pages if p["class"] == k) for k in ("first_party", "marketplace", "unknown")}
    return {"fetched": n, "counts": counts, "stopped": result["stopped"],
            "private_label_slug_pages": sum(1 for p in pages if p["private_label_slug"]),
            "private_label_that_are_first_party": sum(1 for p in pages if p["private_label_slug"] and p["class"] == "first_party")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitemaps", nargs="+", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    urls = urls_from_sitemaps(args.sitemaps)
    pl = sum(1 for u in urls if is_private_label(u))
    offline = {"urls_in_sitemaps": len(urls), "private_label_slug_urls": pl,
               "private_label_slug_share": round(pl / len(urls), 5) if urls else None}
    sample = draw_sample(urls, args.n, args.seed)
    with httpx.Client(timeout=40, headers={"User-Agent": UA, "Accept": "text/html"}, follow_redirects=True,
                      max_redirects=3) as client:
        result = run(sample, client, budget=args.n)
    print(json.dumps({"offline": offline, "sample_seed": args.seed, "summary": summarize(result),
                      "pages": [{k: v for k, v in p.items() if k != "url"} | {"slug": slug(p["url"])[:48]}
                                for p in result["pages"]]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

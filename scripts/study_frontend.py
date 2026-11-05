#!/usr/bin/env python3
"""Study how a retail site records a visitor's chosen store, by reading its PUBLIC static JavaScript.

Descriptive only. It answers: which cookie names, storage keys, URL parameters and endpoints does the site's own
front-end code use for "choose a store", and are those endpoints allowed by robots.txt?

Boundaries (enforced in code, covered by tests):
  * Only Next.js static chunks are requested (path contains /_next/static/ and ends in .js). Anything else on the
    page, including bot-management scripts, is never fetched.
  * robots.txt of the file's own host is read first and honoured for every URL.
  * Identified User-Agent, no cookies or headers set by us, >= 5 s between requests, hard ceiling of 30 files per
    site, stop on the first non-200 response or HTML where JavaScript was expected (a challenge).
  * Downloaded files stay under raw/ (git-ignored). Nothing from them is committed except short identifiers
    (cookie names, parameter names, endpoint paths).
  * Finding an endpoint here never means we call it: endpoints that robots.txt disallows are recorded as off-limits.

  python scripts/study_frontend.py fetch   --page raw/spike_retailers/heb_0.html --base https://www.heb.com/ --out raw/frontend/heb
  python scripts/study_frontend.py analyze --dir raw/frontend/heb --site https://www.heb.com
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx

_here = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("spike_retailers", _here / "spike_retailers.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)

UA, DELAY = sr.UA, sr.DELAY
MAX_PER_SITE = 30
STATIC_MARKER = "/_next/static/"


class Stop(Exception):
    """First sign we should not continue: block, challenge, robots, or an unexpected response."""


# ---- which files may be fetched ---------------------------------------------------------------

def script_urls(html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Split a page's <script src> URLs into (fetchable, excluded). Only Next.js static chunks are fetchable."""
    fetchable, excluded = [], []
    for src in re.findall(r'<script[^>]+src="([^"]+)"', html, re.I):
        url = urljoin(base_url, src)
        path = urlparse(url).path
        ok = url.startswith("https://") and STATIC_MARKER in path and path.endswith(".js")
        (fetchable if ok else excluded).append(url)
    seen = set()
    return [u for u in fetchable if not (u in seen or seen.add(u))], excluded


def fetch_chunks(client: httpx.Client, urls: list[str], out_dir: pathlib.Path,
                 budget: int = MAX_PER_SITE, delay: float = DELAY) -> dict:
    """Politely download chunks. Stops at the first problem; never retries."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"fetched": [], "skipped_existing": 0, "stopped": None}
    robots: dict[str, urllib.robotparser.RobotFileParser] = {}
    first = True
    for url in urls[:min(budget, MAX_PER_SITE)]:
        parts = urlparse(url)
        if STATIC_MARKER not in parts.path or not parts.path.endswith(".js"):
            result["stopped"] = f"refused to fetch a non-static-chunk URL: {parts.path[:60]}"
            return result
        dest = out_dir / pathlib.PurePosixPath(parts.path).name
        if dest.exists() and dest.stat().st_size > 0:
            result["skipped_existing"] += 1
            continue
        if parts.netloc not in robots:
            if not first:
                time.sleep(delay)
            robots[parts.netloc] = sr.robots_for(client, parts.netloc)
            first = False
        if not robots[parts.netloc].can_fetch(UA, url):
            result["stopped"] = f"robots.txt of {parts.netloc} disallows {parts.path[:60]}"
            return result
        if not first:
            time.sleep(delay)
        first = False
        r = client.get(url)
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code != 200:
            result["stopped"] = f"HTTP {r.status_code} for {dest.name}. Stopped; no retry."
            return result
        if "javascript" not in ctype or r.content[:1] == b"<":
            result["stopped"] = f"got non-JavaScript content ({ctype or 'no type'}) for {dest.name}: possible challenge. Stopped; no retry."
            return result
        dest.write_bytes(r.content)
        result["fetched"].append({"file": dest.name, "bytes": len(r.content)})
    return result


# ---- what to look for -------------------------------------------------------------------------

COOKIE_PATTERNS = [
    re.compile(r"""document\.cookie\s*=\s*[`"']([A-Za-z0-9_.\-]+)="""),
    re.compile(r"""\b(?:Cookies?|cookies?|cookieStore)\.(?:set|get|remove)\(\s*["'`]([A-Za-z0-9_.\-]+)["'`]"""),
    re.compile(r"""\b(?:set|get|remove|delete)Cookie\(\s*["'`]([A-Za-z0-9_.\-]+)["'`]"""),
]
STORAGE_RE = re.compile(r"""\b(?:local|session)Storage\.(?:setItem|getItem|removeItem)\(\s*["'`]([^"'`]+)["'`]""")
PARAM_PATTERNS = [
    re.compile(r"""searchParams\.(?:get|set|append|has|delete)\(\s*["'`]([A-Za-z0-9_.\-]+)["'`]"""),
    re.compile(r"""[?&]([A-Za-z0-9_]*[Ss]tore[A-Za-z0-9_]*)="""),
]
ENDPOINT_RE = re.compile(
    r"""["'`](/(?:api|graphql|_next/data|[a-z0-9\-]+/api|ajax|[a-z0-9\-]+/ajax)[A-Za-z0-9_\-/.]*)["'`]""")
ABS_URL_RE = re.compile(r"""["'`](https://[A-Za-z0-9.\-]+(?:/[A-Za-z0-9_\-/.]*)?)["'`]""")
# An absolute URL counts as an endpoint when its host looks like an API host or its path looks like one.
ABS_ENDPOINT_HINT = re.compile(r"(?i)(^https://(api|gateway|graph|services?|edge)[.\-])|/(api|graphql|ajax|stores?|locations?)\b")
STORE_FUNC_RE = re.compile(
    r"""\b((?:set|select|change|update|save|choose)(?:Preferred|Current|My|Selected|Pickup|Shopping)?Stores?[A-Za-z]*)\b""")


def analyze_js(text: str) -> dict:
    """Identifiers only: names of cookies, storage keys, URL parameters, endpoint paths and store functions."""
    cookies = {m for p in COOKIE_PATTERNS for m in p.findall(text)}
    storage = set(STORAGE_RE.findall(text))
    params = {m for p in PARAM_PATTERNS for m in p.findall(text)}
    endpoints = set(ENDPOINT_RE.findall(text))
    endpoints |= {u for u in ABS_URL_RE.findall(text) if ABS_ENDPOINT_HINT.search(u)}
    funcs = set(STORE_FUNC_RE.findall(text))
    return {"cookies": sorted(cookies), "storage_keys": sorted(storage), "url_params": sorted(params),
            "endpoints": sorted(endpoints), "store_functions": sorted(funcs)}


def store_relevant(name: str) -> bool:
    return bool(re.search(r"(?i)store|location|zip|postal|pickup", name))


def merge_findings(parts: list[dict]) -> dict:
    keys = ("cookies", "storage_keys", "url_params", "endpoints", "store_functions")
    return {k: sorted({v for p in parts for v in p.get(k, [])}) for k in keys}


def endpoint_verdicts(endpoints: list[str], site_host: str,
                      robots_by_host: dict[str, urllib.robotparser.RobotFileParser]) -> list[dict]:
    """For each endpoint: which host it is on and whether that host's robots.txt allows us to request it."""
    out = []
    for ep in endpoints:
        host = urlparse(ep).netloc or site_host
        path = urlparse(ep).path or ep
        rp = robots_by_host.get(host)
        if rp is None:
            verdict = "unknown (robots.txt of that host not read; treat as off-limits)"
        else:
            verdict = "allowed" if rp.can_fetch(UA, f"https://{host}{path}") else "DISALLOWED by robots.txt"
        out.append({"endpoint": ep, "host": host, "robots": verdict})
    return out


def contexts(text: str, term: str, width: int = 110, limit: int = 3) -> list[str]:
    """Short snippets around a term, for reading locally. Not for committing."""
    out = []
    for m in re.finditer(re.escape(term), text):
        out.append(re.sub(r"\s+", " ", text[max(0, m.start() - width): m.end() + width]))
        if len(out) >= limit:
            break
    return out


# ---- CLI -----------------------------------------------------------------------------------

def cmd_fetch(args) -> int:
    html = pathlib.Path(args.page).read_text()
    fetchable, excluded = script_urls(html, args.base)
    print(json.dumps({"fetchable": len(fetchable), "excluded_never_fetched": [urlparse(u).path[:50] for u in excluded]}))
    with httpx.Client(timeout=60, headers={"User-Agent": UA, "Accept": "*/*"}, follow_redirects=True,
                      max_redirects=3) as client:
        result = fetch_chunks(client, fetchable, pathlib.Path(args.out), budget=args.budget)
    print(json.dumps(result, indent=2))
    return 1 if result["stopped"] else 0


def cmd_analyze(args) -> int:
    files = sorted(pathlib.Path(args.dir).glob("*.js"))
    per_file = {f.name: analyze_js(f.read_text(errors="replace")) for f in files}
    merged = merge_findings(list(per_file.values()))
    relevant = {k: [v for v in vals if store_relevant(v)] if k != "endpoints" else vals for k, vals in merged.items()}
    print(json.dumps({"files_read": len(files), "store_relevant": relevant}, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--page", required=True)
    f.add_argument("--base", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--budget", type=int, default=MAX_PER_SITE)
    f.set_defaults(fn=cmd_fetch)
    a = sub.add_parser("analyze")
    a.add_argument("--dir", required=True)
    a.set_defaults(fn=cmd_analyze)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

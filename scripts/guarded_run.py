#!/usr/bin/env python3
"""Guarded runner for the Walmart seller check on the Unraid + VPN box.

It turns the runbook's rules into code, so they do not depend on anyone remembering them:

  1. HOME_IP must be set to your real public address.
  2. A recent block means no run: 24 h after a block or challenge, 1 h after any other run (state file).
  3. Traffic must leave through the VPN: the public IP seen from inside the container must differ from HOME_IP.
     If the IP echo service cannot be reached at all, that is treated as "tunnel down" and nothing else runs.
  4. Only then: fetch the two sitemap files if missing (robots.txt checked), draw a seeded sample and read it
     with the same polite client as scripts/spike_walmart_sellers.py (stop on first block, no retry).

  python scripts/guarded_run.py egress     # only the VPN check
  python scripts/guarded_run.py run        # the full guarded check

Exit codes: 0 finished cleanly | 1 stopped (block, challenge, robots) | 2 configuration or state problem |
            3 VPN/egress check failed | 4 refusing: cooldown.

Nothing here prints HOME_IP, and no IP address is written to the output files.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import ipaddress
import json
import os
import pathlib
import sys
import time
from urllib.parse import urlparse

import httpx

_here = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("spike_walmart_sellers", _here / "spike_walmart_sellers.py")
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)
sr = ws.sr

IP_ECHO_URL = "https://api.ipify.org"
SITEMAP_URLS = [
    "https://www.walmart.com/sitemap_product_03_1.xml.gz",
    "https://www.walmart.com/sitemap_itp_01_1.xml.gz",
]
EXIT_OK, EXIT_STOPPED, EXIT_CONFIG, EXIT_EGRESS, EXIT_COOLDOWN = 0, 1, 2, 3, 4


class GuardError(Exception):
    pass


class Blocked(GuardError):
    """A retailer answered with a block or challenge: stop and start the cooldown."""


# ---- VPN egress ------------------------------------------------------------------------------

def parse_ip(text: str):
    try:
        return ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None


def fetch_egress_ip(client: httpx.Client, url: str = IP_ECHO_URL) -> str:
    try:
        r = client.get(url, timeout=15)
    except httpx.HTTPError as e:
        raise GuardError(f"could not reach the IP echo service ({type(e).__name__}); the tunnel may be down") from e
    if r.status_code != 200:
        raise GuardError(f"IP echo service returned HTTP {r.status_code}")
    ip = parse_ip(r.text)
    if ip is None:
        raise GuardError("IP echo service did not return an IP address")
    return str(ip)


def check_egress(egress: str, home: str | None) -> tuple[bool, str]:
    h = parse_ip(home or "")
    if h is None:
        return False, "HOME_IP is missing or not a valid IP address"
    if not h.is_global:
        return False, "HOME_IP is not a public address (use your public IP, not a 192.168.x.x or 10.x.x.x address)"
    e = parse_ip(egress)
    if e is None:
        return False, "egress address is not a valid IP"
    if e == h:
        return False, "public IP equals HOME_IP: traffic is NOT going through the VPN"
    if not e.is_global:
        return False, "egress address is not a public address"
    return True, "traffic leaves via a different public IP than HOME_IP"


# ---- cooldown --------------------------------------------------------------------------------

def cooldown_decision(state: dict | None, now: dt.datetime, hours_after_block: float = 24.0,
                      min_gap_hours: float = 1.0):
    """Return (allowed, retry_at, why). Fails closed on an unreadable timestamp."""
    if not state:
        return True, None, "no previous run recorded"
    try:
        last = dt.datetime.fromisoformat(state["last_run_at"])
        if last.tzinfo is None:
            raise ValueError("naive timestamp")
    except (KeyError, ValueError, TypeError) as e:
        raise GuardError("state file has an unreadable last_run_at; fix or delete it deliberately") from e
    blocked = bool(state.get("blocked"))
    retry_at = last + dt.timedelta(hours=hours_after_block if blocked else min_gap_hours)
    if now < retry_at:
        kind = "the last run was blocked" if blocked else "the last run was too recent"
        return False, retry_at, f"{kind}; next run allowed after {retry_at.isoformat()}"
    return True, None, "cooldown elapsed"


def load_state(path: pathlib.Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (ValueError, OSError) as e:
        raise GuardError(f"state file {path} is unreadable; fix or delete it deliberately") from e


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


# ---- sitemaps --------------------------------------------------------------------------------

def ensure_sitemaps(client: httpx.Client, directory: pathlib.Path, urls: list[str]) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    rp = None
    paths = []
    for url in urls:
        dest = directory / pathlib.PurePosixPath(urlparse(url).path).name
        if dest.exists() and dest.stat().st_size > 0:
            paths.append(str(dest))
            continue
        if rp is None:
            rp = sr.robots_for(client, urlparse(url).netloc)
            time.sleep(sr.DELAY)
        if not rp.can_fetch(sr.UA, url):
            raise GuardError(f"robots.txt disallows {urlparse(url).path}")
        r = client.get(url)
        if r.status_code in sr.BLOCK_STATUSES or r.status_code != 200:
            raise Blocked(f"HTTP {r.status_code} fetching a sitemap")
        if b"<html" in r.content[:300].lower():
            raise Blocked("received an HTML page instead of a sitemap (challenge?)")
        dest.write_bytes(r.content)
        paths.append(str(dest))
        time.sleep(sr.DELAY)
    return paths


# ---- the guarded run -------------------------------------------------------------------------

def egress_only(args, http) -> int:
    if parse_ip(args.home_ip or "") is None:
        print("Configuration error: HOME_IP is missing or not a valid IP address.")
        return EXIT_CONFIG
    try:
        egress = fetch_egress_ip(http, args.ip_echo_url)
    except GuardError as e:
        print(f"Egress check FAILED: {e}")
        return EXIT_EGRESS
    ok, why = check_egress(egress, args.home_ip)
    print(("Egress OK: " if ok else "Egress check FAILED: ") + why)
    return EXIT_OK if ok else EXIT_EGRESS


def run_check(args, http, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    if parse_ip(args.home_ip or "") is None:
        print("Configuration error: HOME_IP is missing or not a valid IP address.")
        return EXIT_CONFIG
    try:
        state = load_state(args.state)
        allowed, _retry, why = cooldown_decision(state, now, args.cooldown_hours)
    except GuardError as e:
        print(f"Configuration error: {e}")
        return EXIT_CONFIG
    if not allowed:
        print(f"Refusing to run: {why}")
        return EXIT_COOLDOWN

    try:
        egress = fetch_egress_ip(http, args.ip_echo_url)
    except GuardError as e:
        print(f"Egress check FAILED: {e}")
        return EXIT_EGRESS
    ok, why = check_egress(egress, args.home_ip)
    if not ok:
        print(f"Egress check FAILED: {why}")
        return EXIT_EGRESS
    print(f"Egress OK: {why}")

    try:
        paths = ensure_sitemaps(http, args.sitemap_dir, args.sitemap_urls)
        urls = ws.urls_from_sitemaps(paths)
        if not urls:
            raise GuardError("no /ip/ URLs found in the sitemap files")
        sample = ws.draw_sample(urls, args.n, args.seed)
        result = ws.run(sample, http, budget=args.n)
    except Blocked as e:
        result = {"pages": [], "stopped": f"blocked: {e}. Stopped; no retry."}
    except GuardError as e:
        print(f"Stopped: {e}")
        return EXIT_CONFIG

    stopped = result["stopped"]
    blocked = bool(stopped and ("challenge" in stopped or "blocked" in stopped))
    summary = ws.summarize(result)
    out = {
        "started_at": now.isoformat(), "n_requested": args.n, "seed": args.seed, "egress_check": "ok",
        "summary": summary, "stopped": stopped,
        "pages": [{k: v for k, v in p.items() if k != "url"} | {"slug": ws.slug(p.get("url", ""))[:48]}
                  for p in result["pages"]],
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"walmart_sellers_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    save_state(args.state, {"last_run_at": now.isoformat(), "blocked": blocked, "stopped": stopped})
    print(json.dumps({"summary": summary, "stopped": stopped, "output_file": out_path.name}, indent=2))
    if blocked:
        print(f"A block or challenge was recorded. The next run is refused for {args.cooldown_hours:g} hours. "
              "Do not work around it.")
    return EXIT_STOPPED if stopped else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["egress", "run"])
    ap.add_argument("--home-ip", default=env("HOME_IP"))
    ap.add_argument("--ip-echo-url", default=env("IP_ECHO_URL", IP_ECHO_URL))
    ap.add_argument("--state", type=pathlib.Path, default=pathlib.Path(env("STATE_FILE", "/data/state.json")))
    ap.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path(env("OUT_DIR", "/data/out")))
    ap.add_argument("--sitemap-dir", type=pathlib.Path, default=pathlib.Path(env("SITEMAP_DIR", "/data/sitemaps")))
    ap.add_argument("--sitemap-urls", nargs="+", default=SITEMAP_URLS)
    ap.add_argument("--n", type=int, default=int(env("N_PAGES", "20")))
    ap.add_argument("--seed", type=int, default=int(env("SEED", "7")))
    ap.add_argument("--cooldown-hours", type=float, default=float(env("COOLDOWN_HOURS", "24")))
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    with httpx.Client(timeout=60, headers={"User-Agent": sr.UA, "Accept": "*/*"}, follow_redirects=True,
                      max_redirects=3) as http:
        return egress_only(args, http) if args.command == "egress" else run_check(args, http)


if __name__ == "__main__":
    sys.exit(main())

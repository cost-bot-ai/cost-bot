#!/usr/bin/env python3
"""Kroger-family shelf-price collector (official public API; runs on GitHub Actions).

  python -m collectors.kroger run --dry-run                 # calls the API, writes nothing, needs no database
  python -m collectors.kroger run --max-requests 100        # writes to Postgres (DATABASE_URL), schema must be migrated

What it does, in order:
  1. Reads data/curated/basket.yaml (GTINs) and data/curated/kroger_panel.yaml (pinned stores).
  2. For each pinned store, asks the Products API for the basket in batches (filter.productId + filter.locationId).
  3. Turns each returned product into one observation: 'priced' (regular price, optional promo) or 'unpriced'
     (returned by the API but carrying no price at that store: NOT a price drop).
  4. Writes a row only when something changed (price, promo, size, priced/unpriced) or a weekly heartbeat is due.
     A product the API did not return for a store produces no row at all.

Rules enforced in code (each has a test):
  * Self-cap: stop once the day's remaining quota falls to 50% of the limit the API reports in its own headers.
  * Stop on 403/429 and on any other non-200 (one 401 triggers a single token refresh; a second 401 stops).
    Never retry through a refusal. 429/403 end the run with status 'rate_limited' / 'blocked'.
  * Pace: at least MIN_GAP seconds between requests.
  * Kill switch: retailer.enabled = false in the database means nothing is requested.
  * Secrets: client id/secret and tokens are never printed; the only output is a JSON summary of counts.
  * Facts only: GTIN, product title, brand, size, price, promo, stock level, store id, time. No images or marketing text.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import pathlib
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Callable, Optional, Protocol

import httpx
import yaml

from pipeline.transform.metrics import unit_price_cents

BASE = "https://api.kroger.com/v1"
UA = "PriceMemory/0.1 (price research; +https://github.com/cost-bot-ai/cost-bot)"
SOURCE = "kroger_api"
RETAILER_SLUG = "kroger"

BATCH = 8                       # 8 UPCs per call is the size verified in docs/spikes.md; raise only after measuring
MIN_GAP = 1.0                   # seconds between API requests
DAILY_CAP_FRACTION = 0.5        # stop when remaining quota <= this share of the reported daily limit
HEARTBEAT = timedelta(days=7)   # write an unchanged observation at least this often
TOKEN_MARGIN = 120              # refresh the 30-minute token this many seconds early

EXIT_OK, EXIT_PARTIAL, EXIT_CONFIG, EXIT_STOPPED = 0, 1, 2, 3


class Stop(Exception):
    """The run must end now. `status` is what gets recorded in collection_run."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---- configuration ----------------------------------------------------------------------------

def normalize_gtin(raw) -> str:
    """Digits only, zero-padded to 14 (the form stored in product.gtin). Raises ValueError otherwise."""
    s = str(raw).strip()
    if not s.isdigit() or not 8 <= len(s) <= 14:
        raise ValueError(f"not a GTIN/UPC: {raw!r}")
    return s.zfill(14)


@dataclass(frozen=True)
class BasketItem:
    gtin: str
    name: str = ""
    category: str = ""


@dataclass(frozen=True)
class PanelStore:
    location_id: str
    metro: str
    banner: str
    name: str
    format: Optional[str]      # None = unknown (the file says 'unknown'); never guessed
    role: str = ""


def load_basket(path) -> list[BasketItem]:
    doc = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    items, seen = [], set()
    for p in doc.get("products") or []:
        gtin = normalize_gtin(p["gtin"])
        if gtin in seen:
            raise ValueError(f"duplicate GTIN in basket: {gtin}")
        seen.add(gtin)
        items.append(BasketItem(gtin, str(p.get("name", "")), str(p.get("category", ""))))
    return items


def load_panel(path) -> list[PanelStore]:
    doc = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    stores, seen = [], set()
    for s in doc.get("stores") or []:
        loc = str(s["location_id"]).strip()
        if not loc:
            raise ValueError("panel store without location_id")
        if loc in seen:
            raise ValueError(f"duplicate location_id in panel: {loc}")
        seen.add(loc)
        fmt = str(s.get("format", "unknown"))
        stores.append(PanelStore(loc, str(s["metro"]), str(s.get("banner", "")), str(s.get("name", "")),
                                 None if fmt == "unknown" else fmt, str(s.get("role", ""))))
    return stores


# ---- parsing ----------------------------------------------------------------------------------

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|floz|oz|lb|ct|count|ea|each|dz|kg|g|ml|l|gal|qt|pt)\b", re.I)


def parse_size(text) -> Optional[tuple[float, str]]:
    """First '<number> <unit>' in a free-text size such as '12 ct / 24 oz' (the primary label). None if absent."""
    if not text:
        return None
    m = _SIZE_RE.search(str(text))
    if not m:
        return None
    unit = re.sub(r"\s+", " ", m.group(2).lower().replace(".", ""))
    return float(m.group(1)), "fl oz" if unit == "floz" else unit


def to_cents(value) -> Optional[int]:
    """Dollars -> integer cents, rounded half up. None when absent. ValueError on junk or negatives."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"bad price {value!r}")
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"bad price {value!r}") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"bad price {value!r}")
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Observation:
    gtin: str
    external_id: str            # Kroger productId
    name: str
    brand: str
    size_value: Optional[float]
    size_unit: Optional[str]
    status: str                 # 'priced' | 'unpriced'
    price_cents: Optional[int]
    promo_cents: Optional[int]
    unit_price: Optional[float]  # cents per canonical unit (oz / fl oz / ct)
    stock_level: Optional[str]
    location_id: str
    observed_at: datetime


def observation_from_product(p: dict, location_id: str, now: datetime) -> Observation:
    """One API product -> one Observation. Raises ValueError/KeyError when the payload is not usable."""
    gtin = normalize_gtin(p["upc"])
    items = p.get("items") or []
    item = items[0] if items else {}
    price = item.get("price") or {}
    regular, promo = to_cents(price.get("regular")), to_cents(price.get("promo"))
    size = parse_size(item.get("size"))
    status = "priced" if regular is not None else "unpriced"
    if status == "unpriced":
        promo = None
    unit = None
    if status == "priced" and size:
        try:
            unit = unit_price_cents(regular, size[0], size[1])[0]
        except ValueError:
            unit = None
    return Observation(
        gtin=gtin, external_id=str(p.get("productId") or ""), name=str(p.get("description") or "")[:200],
        brand=str(p.get("brand") or "")[:100], size_value=size[0] if size else None,
        size_unit=size[1] if size else None, status=status, price_cents=regular, promo_cents=promo,
        unit_price=unit, stock_level=(item.get("inventory") or {}).get("stockLevel"),
        location_id=location_id, observed_at=now)


@dataclass(frozen=True)
class Previous:
    observed_at: datetime
    status: str
    price_cents: Optional[int]
    promo_cents: Optional[int]
    size_value: Optional[float]
    size_unit: Optional[str]


def _same_size(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-9)


def should_write(prev: Optional[Previous], new: Observation) -> bool:
    """Change-only storage: first sight, any change in what a shopper would notice, or a weekly heartbeat.

    Stock level alone never triggers a write (it flips often and is not a price).
    """
    if prev is None:
        return True
    if (prev.status, prev.price_cents, prev.promo_cents, prev.size_unit) != \
            (new.status, new.price_cents, new.promo_cents, new.size_unit):
        return True
    if not _same_size(prev.size_value, new.size_value):
        return True
    return new.observed_at - prev.observed_at >= HEARTBEAT


# ---- storage interface ------------------------------------------------------------------------

class Repo(Protocol):
    def retailer_enabled(self) -> bool: ...
    def check_schema(self) -> None: ...
    def start_run(self, now: datetime) -> int: ...
    def ensure_store(self, s: PanelStore) -> int: ...
    def ensure_product(self, o: Observation) -> int: ...
    def latest(self, store_id: int, product_ids: list[int]) -> dict[int, Previous]: ...
    def write(self, product_id: int, store_id: int, o: Observation) -> None: ...
    def finish_run(self, run_id: int, now: datetime, status: str, requests: int, rows: int,
                   http_errors: int, note: str) -> None: ...


class MemoryRepo:
    """No database: used by --dry-run and by tests. Remembers what it was given."""

    def __init__(self, enabled: bool = True, previous: Optional[dict] = None):
        self.enabled, self.rows, self.runs = enabled, [], []
        self._previous = previous or {}           # (location_id, gtin) -> Previous
        self._stores, self._products = {}, {}
        self.created_stores: list[PanelStore] = []

    def retailer_enabled(self): return self.enabled
    def check_schema(self): pass

    def start_run(self, now):
        self.runs.append({"started": now})
        return len(self.runs)

    def ensure_store(self, s):
        if s.location_id not in self._stores:
            self._stores[s.location_id] = len(self._stores) + 1
            self.created_stores.append(s)
        return self._stores[s.location_id]

    def ensure_product(self, o):
        return self._products.setdefault(o.gtin, len(self._products) + 1)

    def latest(self, store_id, product_ids):
        loc = next(k for k, v in self._stores.items() if v == store_id)
        by_id = {v: k for k, v in self._products.items()}
        return {pid: self._previous[(loc, by_id[pid])] for pid in product_ids if (loc, by_id[pid]) in self._previous}

    def write(self, product_id, store_id, o):
        self.rows.append(o)

    def finish_run(self, run_id, now, status, requests, rows, http_errors, note):
        self.runs[run_id - 1].update(finished=now, status=status, requests=requests, rows=rows,
                                     http_errors=http_errors, note=note)


# ---- API client -------------------------------------------------------------------------------

class KrogerClient:
    def __init__(self, http: httpx.Client, client_id: str, client_secret: str,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 min_gap: float = MIN_GAP):
        self._http, self._id, self._secret = http, client_id, client_secret
        self._clock, self._sleep, self._min_gap = clock, sleep, min_gap
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._last_request: Optional[float] = None
        self.requests = 0
        self.max_requests: Optional[int] = None      # hard ceiling on HTTP requests, checked before each one
        self.http_errors = 0
        self.limit_day: Optional[int] = None
        self.remaining_day: Optional[int] = None

    # -- pacing and accounting
    def _pace(self) -> None:
        # Called before every HTTP request (token, products, and the retry after a 401): the ceiling is never exceeded.
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise Stop("partial", f"stopped at --max-requests {self.max_requests}")
        if self._last_request is not None:
            wait = self._min_gap - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()
        self.requests += 1

    def _note_headers(self, r: httpx.Response) -> None:
        for attr, header in (("limit_day", "x-ratelimit-limit-day"), ("remaining_day", "x-ratelimit-remaining-day")):
            try:
                setattr(self, attr, int(r.headers[header]))
            except (KeyError, ValueError):
                pass

    def over_cap(self) -> bool:
        """True once the remaining daily quota is at or below the reserved share of the reported limit."""
        if self.limit_day is None or self.remaining_day is None:
            return False
        return self.remaining_day <= self.limit_day * DAILY_CAP_FRACTION

    # -- token
    def _fetch_token(self) -> None:
        self._pace()
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        r = self._http.post(f"{BASE}/connect/oauth2/token", data={"grant_type": "client_credentials",
                            "scope": "product.compact"}, headers={"Authorization": f"Basic {basic}"})
        if r.status_code != 200:
            self.http_errors += 1
            raise Stop("blocked" if r.status_code in (401, 403) else "error", f"token request returned HTTP {r.status_code}")
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = self._clock() + float(body.get("expires_in", 1800)) - TOKEN_MARGIN

    def _auth(self) -> dict:
        if self._token is None or self._clock() >= self._token_expires:
            self._fetch_token()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    # -- products
    def products(self, location_id: str, gtins: list[str]) -> list[dict]:
        params = {"filter.productId": ",".join(gtins), "filter.locationId": location_id, "filter.limit": 50}
        refreshed = False
        while True:
            headers = self._auth()
            self._pace()
            r = self._http.get(f"{BASE}/products", params=params, headers=headers)
            self._note_headers(r)
            if r.status_code == 200:
                return r.json().get("data") or []
            self.http_errors += 1
            if r.status_code == 401 and not refreshed:
                refreshed, self._token = True, None      # the 30-minute token may have lapsed: refresh once
                continue
            if r.status_code == 429:
                raise Stop("rate_limited", "HTTP 429 from the Products API")
            if r.status_code in (401, 403):
                raise Stop("blocked", f"HTTP {r.status_code} from the Products API")
            raise Stop("error", f"HTTP {r.status_code} from the Products API")


# ---- the run ----------------------------------------------------------------------------------

def api_upc(gtin14: str) -> str:
    """The 13-digit form Kroger returns and the spike verified (zero-padded UPC-A); the 14-digit form is untested."""
    return gtin14[1:] if gtin14.startswith("0") else gtin14


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run(basket: list[BasketItem], panel: list[PanelStore], client: KrogerClient, repo: Repo,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), max_requests: int = 1500,
        batch: int = BATCH) -> dict:
    result = {"status": "ok", "requests": 0, "stores": len(panel), "basket": len(basket), "returned": 0,
              "priced": 0, "unpriced": 0, "rows_written": 0, "unchanged": 0, "bad_rows": 0, "http_errors": 0,
              "note": ""}
    if not repo.retailer_enabled():
        result.update(status="disabled", note="retailer kill switch is off; nothing requested")
        return result
    if not basket or not panel:
        result.update(status="error", note="basket or panel is empty")
        return result
    repo.check_schema()
    client.max_requests = max_requests
    run_id = repo.start_run(now())
    try:
        # Inside the protected block: a failure here must still close the run row ('running' is never left behind).
        store_ids = {s.location_id: repo.ensure_store(s) for s in panel}
        # Outer loop = batch, inner = store: if the run stops early, every store still has its first batches.
        for chunk in chunks([b.gtin for b in basket], batch):
            for s in panel:
                if client.over_cap():
                    raise Stop("partial", f"stopped at the {int(DAILY_CAP_FRACTION * 100)}% daily-quota reserve")
                if client.requests >= max_requests:
                    raise Stop("partial", f"stopped at --max-requests {max_requests}")
                data = client.products(s.location_id, [api_upc(g) for g in chunk])
                observed = now()
                obs: list[Observation] = []
                for p in data:
                    try:
                        o = observation_from_product(p, s.location_id, observed)
                    except (KeyError, ValueError, TypeError):
                        result["bad_rows"] += 1
                        continue
                    if o.gtin not in chunk:           # the API returned something we did not ask for: ignore it
                        result["bad_rows"] += 1
                        continue
                    obs.append(o)
                result["returned"] += len(obs)
                pids = {o.gtin: repo.ensure_product(o) for o in obs}
                prev = repo.latest(store_ids[s.location_id], list(pids.values()))
                for o in obs:
                    result["priced" if o.status == "priced" else "unpriced"] += 1
                    if should_write(prev.get(pids[o.gtin]), o):
                        repo.write(pids[o.gtin], store_ids[s.location_id], o)
                        result["rows_written"] += 1
                    else:
                        result["unchanged"] += 1
    except Stop as stop:
        result["status"], result["note"] = stop.status, stop.message
    except BaseException as unexpected:
        result["status"], result["note"] = "error", f"unexpected {type(unexpected).__name__}"
        raise
    finally:
        result["requests"], result["http_errors"] = client.requests, client.http_errors
        final = "ok" if result["status"] == "ok" else result["status"]
        repo.finish_run(run_id, now(), final, result["requests"], result["rows_written"], result["http_errors"],
                        result["note"])
    return result


# ---- Postgres ---------------------------------------------------------------------------------

class PgRepo:
    """Postgres via psycopg 3. Parameterised SQL only. The caller owns the connection (autocommit off)."""

    def __init__(self, conn):
        self.conn = conn

    def _one(self, sql, args=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def check_schema(self):
        row = self._one("SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                        "AND table_name='price_obs' AND column_name IN ('status','stock_level')")
        if not row or row[0] != 2:
            raise SystemExit("Database schema is not migrated to 002 (run scripts/migrate.py first).")

    def retailer_enabled(self):
        row = self._one("SELECT enabled FROM retailer WHERE slug=%s", (RETAILER_SLUG,))
        if row is None:
            self._one("INSERT INTO retailer (slug, name) VALUES (%s, %s) ON CONFLICT (slug) DO NOTHING RETURNING id",
                      (RETAILER_SLUG, "Kroger family (official API)"))
            self.conn.commit()
            return True
        return bool(row[0])

    def _retailer_id(self):
        return self._one("SELECT id FROM retailer WHERE slug=%s", (RETAILER_SLUG,))[0]

    def start_run(self, now):
        rid = self._one("INSERT INTO collection_run (retailer_id, started_at, status) VALUES (%s, %s, 'running') "
                        "RETURNING id", (self._retailer_id(), now))[0]
        self.conn.commit()
        return rid

    def ensure_store(self, s):
        rid = self._retailer_id()
        self._one("INSERT INTO store (retailer_id, external_id, banner, metro, format) VALUES (%s,%s,%s,%s,%s) "
                  "ON CONFLICT (retailer_id, external_id) DO NOTHING RETURNING id",
                  (rid, s.location_id, s.banner, s.metro, s.format))
        sid = self._one("SELECT id FROM store WHERE retailer_id=%s AND external_id=%s", (rid, s.location_id))[0]
        self.conn.commit()
        return sid

    def ensure_product(self, o):
        brand_id = None
        if o.brand:
            self._one("INSERT INTO brand (name, source) VALUES (%s, 'kroger_api') ON CONFLICT (name) DO NOTHING "
                      "RETURNING id", (o.brand,))
            brand_id = self._one("SELECT id FROM brand WHERE name=%s", (o.brand,))[0]
        self._one("INSERT INTO product (gtin, name, brand_id, size_value, size_unit) VALUES (%s,%s,%s,%s,%s) "
                  "ON CONFLICT (gtin) DO NOTHING RETURNING id", (o.gtin, o.name or o.gtin, brand_id, o.size_value, o.size_unit))
        pid = self._one("SELECT id FROM product WHERE gtin=%s", (o.gtin,))[0]
        if o.external_id:
            self._one("INSERT INTO product_external_id (product_id, retailer_id, external_id) VALUES (%s,%s,%s) "
                      "ON CONFLICT (retailer_id, external_id) DO NOTHING RETURNING id",
                      (pid, self._retailer_id(), o.external_id))
        self.conn.commit()
        return pid

    def latest(self, store_id, product_ids):
        if not product_ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT ON (product_id) product_id, observed_at, status, price_cents, promo_cents, "
                        "size_value, size_unit FROM price_obs WHERE store_id=%s AND product_id = ANY(%s) "
                        "ORDER BY product_id, observed_at DESC", (store_id, product_ids))
            return {r[0]: Previous(r[1], r[2], r[3], r[4], None if r[5] is None else float(r[5]), r[6])
                    for r in cur.fetchall()}

    def write(self, product_id, store_id, o):
        self._one("INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, promo_cents, size_value, "
                  "size_unit, unit_price, source, status, stock_level) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                  "RETURNING id", (product_id, store_id, o.observed_at, o.price_cents, o.promo_cents, o.size_value,
                                   o.size_unit, o.unit_price, SOURCE, o.status, o.stock_level))
        self.conn.commit()

    def finish_run(self, run_id, now, status, requests, rows, http_errors, note):
        self.conn.rollback()      # a failed statement leaves the transaction aborted; clear it so the run can be closed
        self._one("UPDATE collection_run SET finished_at=%s, status=%s, requests=%s, rows_written=%s, http_errors=%s, "
                  "note=%s WHERE id=%s RETURNING id", (now, status, requests, rows, http_errors, note[:500], run_id))
        self.conn.commit()


# ---- CLI --------------------------------------------------------------------------------------

def exit_code(result: dict) -> int:
    return {"ok": EXIT_OK, "disabled": EXIT_OK, "partial": EXIT_PARTIAL, "error": EXIT_PARTIAL,
            "rate_limited": EXIT_STOPPED, "blocked": EXIT_STOPPED}.get(result["status"], EXIT_PARTIAL)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--basket", default="data/curated/basket.yaml")
    r.add_argument("--panel", default="data/curated/kroger_panel.yaml")
    r.add_argument("--max-requests", type=int, default=1500)
    r.add_argument("--dry-run", action="store_true", help="call the API but write nothing and use no database")
    args = ap.parse_args(argv)

    cid, secret = os.environ.get("KROGER_CLIENT_ID"), os.environ.get("KROGER_CLIENT_SECRET")
    if not cid or not secret:
        print("Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET (never commit them).", file=sys.stderr)
        return EXIT_CONFIG
    try:
        basket, panel = load_basket(args.basket), load_panel(args.panel)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
        print(f"Bad configuration: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_CONFIG

    conn = None
    if args.dry_run:
        repo: Repo = MemoryRepo()
    else:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            print("Set DATABASE_URL (or use --dry-run).", file=sys.stderr)
            return EXIT_CONFIG
        import psycopg
        conn = psycopg.connect(dsn)
        repo = PgRepo(conn)
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": UA}) as http:
            result = run(basket, panel, KrogerClient(http, cid, secret), repo, max_requests=args.max_requests)
    finally:
        if conn is not None:
            conn.close()
    result["dry_run"] = args.dry_run
    print(json.dumps(result, indent=2))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())

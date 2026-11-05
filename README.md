# PriceMemory (working name)

A free-to-run site that shows where consumer prices rose faster than producer costs, which
companies' margins expanded, and what the same product costs across stores and metros over time.

Full plan: see the approved plan (Context, research, phases, guardrails). This README tracks status.

## Status

| Phase | State |
|---|---|
| 0 Skeleton | **Done:** repo, schema (validated on Postgres 16), metric math (31 tests), curated-data stubs. |
| 0 Spikes | **Done:** Kroger (API, the multi-metro source); Open Prices coverage; retailer page tests; Unraid setup (`deploy/unraid/`, awaiting the owner's Walmart run); front-end study of H-E-B and Whole Foods: **neither can be priced per store by sanctioned means** (H-E-B's store change is a disallowed GraphQL call; Whole Foods uses internal APIs). Target and Aldi disallow crawling; Albertsons/Safeway challenge. Not done: Neon-over-HTTP. See `docs/spikes.md`. |
| 3 Kroger collector | **Built, not yet scheduled:** `collectors/kroger.py` (+ 70 tests, 8 of them against a real Postgres), manual workflow `kroger.yml`. A live dry-run matched the spike. Needs `DATABASE_URL` / Kroger secrets as Actions secrets before a real run. Basket is an 8-UPC seed. |
| 1 Gov categories | Not started. Series IDs in `data/curated/series.yaml` are **unverified**. |
| 2+ | Not started. |

## Walmart check on your own network (Unraid + Proton VPN)

`deploy/unraid/` holds a Compose file (VPN container + sampler sharing its network), a Dockerfile, an example settings
file and a step-by-step `RUNBOOK.md`. `scripts/guarded_run.py` enforces the rules in code: the traffic must leave
through the VPN, a block means 24 hours of refusal, and nothing runs by default. Read the runbook, including the
kill-switch drill, before running anything.

## Database

Schema lives in `db/migrations/` (forward-only; never edit an applied file, add a new one).

```bash
pip install -e '.[db]'
export DATABASE_URL=postgresql://...        # never commit this
python scripts/migrate.py                   # applies pending migrations in order, once each
```

Schema tests need a real Postgres and are skipped without one. CI runs them against `postgres:16` on every PR.

```bash
export TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres   # a throwaway database
python -m pytest tests/test_schema.py
```

## Kroger collector

```bash
export KROGER_CLIENT_ID=... KROGER_CLIENT_SECRET=...      # never commit these
python -m collectors.kroger run --dry-run                  # calls the API, writes nothing, needs no database
export DATABASE_URL=postgresql://...                       # a migrated database (scripts/migrate.py)
python -m collectors.kroger run --max-requests 100
```

It prints only a JSON summary of counts. In GitHub, use Actions -> kroger-collector -> Run workflow (dry run is the default).

## Run the tests

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
python -m pytest -q
```

## Run the Kroger spike (once credentials and network access exist)

```bash
export KROGER_CLIENT_ID=... KROGER_CLIENT_SECRET=...   # never commit these
python scripts/spike_kroger.py --term "large eggs" --metros cincinnati,los-angeles,denver
```

It prints rate-limit headers, whether multi-ID batching works, and same-product prices
across metros, and saves raw payloads under `raw/spike_kroger/` (git-ignored).

## Ground rules

- Facts only (price, size, store, date, SKU). No retailer images, descriptions or logos.
- Collectors stop on any 401/403/429 or challenge page; no CAPTCHA solving, fingerprint
  spoofing, proxy rotation or logins.
- Neutral metric names and visible formulas; no accusatory labels.
- Series are never published until `status: verified` in `data/curated/series.yaml`.

# PriceMemory (working name)

A free-to-run site that shows where consumer prices rose faster than producer costs, which
companies' margins expanded, and what the same product costs across stores and metros over time.

Full plan: see the approved plan (Context, research, phases, guardrails). This README tracks status.

## Status

| Phase | State |
|---|---|
| 0 Skeleton | **Done:** repo, schema (validated on Postgres 16), metric math (31 tests), curated-data stubs. |
| 0 Spikes | **Done:** Kroger (API); Open Prices coverage; page tests on the added retailers. **Walmart:** a first-party flag exists (`sellerType`), but its bot protection challenged this cloud IP on the third page, so access is unproven (see `docs/spikes.md`). **H-E-B** price+GTIN readable; **Whole Foods** no price until a store is chosen; **no sanctioned store-selection mechanism found** on either. **Albertsons/Safeway** challenge; **Aldi** and **Target** (API host) disallow crawling. **Not done:** Neon-over-HTTP, re-run from the Unraid/Proton path. |
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

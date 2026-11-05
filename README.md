# PriceMemory (working name)

A free-to-run site that shows where consumer prices rose faster than producer costs, which
companies' margins expanded, and what the same product costs across stores and metros over time.

Full plan: see the approved plan (Context, research, phases, guardrails). This README tracks status.

## Status

| Phase | State |
|---|---|
| 0 Skeleton | **Done:** repo, schema (validated on Postgres 16), metric math (31 tests), curated-data stubs. |
| 0 Spikes | **Kroger: done** (`docs/spikes.md`). **Target/Walmart: done once** from a cloud IP: Walmart page carries the price, Target page does not (its price API is a pending decision). Open Prices stats and Neon-over-HTTP: not started. |
| 1 Gov categories | Not started. Series IDs in `data/curated/series.yaml` are **unverified**. |
| 2+ | Not started. |

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

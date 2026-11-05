# Handoff (written 2026-11-05 for the next agent/node)

Read this first. Measurements are in `docs/spikes.md` and owner decisions in `docs/decisions.md`; those are the source of truth.
The original long-form plan (phases, architecture, metrics) lived outside the repo; the essentials are summarised below and in `README.md`.

## 1. Who/what/where
- **Owner:** tim@job-bot.ai. Repo: `cost-bot-ai/cost-bot` (public). Working dir in cloud sessions: `/home/user/cost-bot`.
- **Goal:** a free-to-run consumer site showing (a) retail price vs producer-cost "wedge" by category, (b) company margin
  expansion vs 2019 (SEC XBRL), (c) shelf-price history of the same product across stores/metros (Kroger API first),
  (d) crowd prices (Open Food Facts "Open Prices"). Cost target ~$0 (Neon free Postgres, static site on Cloudflare
  Pages + thin Worker, GitHub Actions for official APIs, owner's Unraid box for anything needing a home-network path).
- **State:** `main` @ `e5e936b`. PRs #1-#11 all merged. Phase 0 (skeleton + spikes) is complete except Neon-over-HTTP.
  No site, no production collector, no government-data pipeline exists yet.
- **Tests:** `python -m pytest -q` => 182 passed, 17 skipped (the 17 are DB tests; set `TEST_DATABASE_URL` to run them;
  CI runs them on `postgres:16`). CI jobs: `pytest`, `schema`, `image` (builds `deploy/unraid/Dockerfile`).

## 2. Owner's standing rules (do not relax without the owner saying so)
1. `robots.txt` honoured in code, fail closed if unreadable. 2. Identified User-Agent
`PriceMemory-spike/0.1 (price research; +https://github.com/cost-bot-ai/cost-bot)`. 3. No CAPTCHA solving, TLS/browser
spoofing, proxy/IP rotation, logins. 4. On the first block/challenge: stop, never retry, back off 24 h. 5. Facts only
(price, size, store, date, SKU); no retailer images/descriptions/logos. 6. Neutral metric names; no accusatory labels
("gouging" etc.); show formulas. 7. Never call robots-disallowed or undocumented internal APIs, even if found in
front-end code. 8. No secrets in chat or repo (names only). 9. Anything outward-facing (merge, push to others' repos,
network allowlist change, installing on the owner's machines) needs the owner's say. Allowlist changes go through the
environment approval prompt and should be removed afterwards.
- **Owner preferences for the assistant:** use the jev tools as default checkpoints (`jev_screen` on fetched content,
  `jev_verify`/`jev_audit` on claims/extracted values, `jev_noul` for probabilities, `jev_decide` for options,
  `jev_gate` before calling code done) and mention a result in one line when it changes the answer, flagging low
  confidence. End any message that needs the owner with a final block `### 🔴 NEEDED FROM YOU` (numbered, bold verb,
  nothing else); omit it when nothing is needed. Report failures and corrections plainly (see section 7).

## 3. Owner decisions so far (full text in `docs/decisions.md`)
- US first, then CA/UK/EU. Both company scorecards and category price pages. Open Prices as crowd source.
- Retailer scraping is IN (owner overrode my first draft); free DB + server-rendered pages are OK. Neon Postgres chosen.
- Walmart: owner first said "entire catalog", then narrowed to **Walmart-first-party items only**
  (`sellerType == INTERNAL`). The full catalog is infeasible (see Walmart below).
- Unraid + Proton WireGuard (gluetun) for retailer fetches that must not use the cloud IP. Owner says Proton's terms on
  automated traffic are read and OK.
- "HEC" = H-E-B. Schema change first (done), Unraid setup (done), front-end study (done).
- Left as is: the PR #10 commit message says "8 rules" where 7 mutations were actually tested (PR body was corrected).

## 4. What exists in the repo (all on `main`)
| Path | What it is |
|---|---|
| `pipeline/transform/metrics.py` + `tests/test_metrics.py` | Rebase to Jan-2020=100, wedge, pass-through, TTM margin (NaN across gaps/duplicates), baseline margin, margin expansion (pp), size normalization, unit price, pct change. 31+ tests. |
| `db/migrations/001_init.sql`, `002_product_equivalence.sql` | Postgres schema. 002 adds `product_group`, `product.group_id`, `product_external_id` (UNIQUE retailer_id+external_id), `store.format/division` (NULL=unknown), `price_obs.status` priced/unpriced with CHECK `(status='priced') = (price_cents IS NOT NULL)`, `stock_level`. |
| `scripts/migrate.py` | Forward-only runner with checksums in `schema_migrations`; refuses if an applied file was edited. Uses `DATABASE_URL`/`--dsn`, never prints it. |
| `scripts/spike_kroger.py` | Kroger probe (OAuth client-credentials, locations, products, batching, cross-metro comparison). |
| `scripts/spike_retailers.py` | Polite product-page client (+ `robots_for`, `classify`, price extraction) for target, walmart, aldi, albertsons, safeway, heb, wholefoods. |
| `scripts/spike_openprices.py` | Open Prices coverage (US) with self-checks. |
| `scripts/spike_walmart_sellers.py` | Seeded sampler: reads `__NEXT_DATA__`, classifies seller INTERNAL/EXTERNAL; MAX_PAGES=25 ceiling. |
| `scripts/guarded_run.py` | Wrapper: `egress` and `run` subcommands; exit codes 0/1/2/3/4; VPN egress IP must differ from `HOME_IP`; 24 h cooldown after block, 1 h min gap, unreadable state fails closed; no IP printed or saved. |
| `scripts/study_frontend.py` | Reads only `/_next/static/*.js` chunks (robots-checked, 5 s, max 30/site, stops on non-200/HTML) and lists store-related cookie/storage/param/endpoint names. Never calls found endpoints. |
| `deploy/unraid/` | `docker-compose.yml` (gluetun + sampler via `network_mode: "service:gluetun"`, read-only, cap_drop ALL, no ports, `restart: "no"`), `Dockerfile` (non-root uid 10001, ENTRYPOINT only, no CMD), `env.example`, `RUNBOOK.md` (9 steps incl. kill-switch drill). |
| `data/curated/` | `kroger_panel.yaml` (15 candidate metros; 3 verified stores), `series.yaml` (BLS IDs from memory, **all unverified**), `basket.yaml` (**empty**). |
| `docs/spikes.md`, `docs/decisions.md` | Every measurement and every owner decision with dates. Read these for numbers; they are the source of truth. |
| `.github/workflows/tests.yml` | The three CI jobs; the `schema` job uses `set -o pipefail` and fails if tests were skipped. |

## 5. Findings per source (details and numbers in `docs/spikes.md`)
- **Kroger API: GO, primary multi-metro source.** Limits from response headers: products 10,000/day, locations 5,000/day.
  `filter.productId` batching works (8 UPCs/call verified). Rough budget ~760 calls/day for 150 SKUs x 40 stores.
  Price is `items[0].price.regular`; a product can come back with no price at a store ("unpriced" - not a drop).
  Filter out non-retail nodes (zero `departments`). Token lasts 1800 s. **Private-label UPCs differ by division**
  (Cincinnati shared 0 of its top-10 egg UPCs with LA/Denver) - hence `product_group`. Same-UPC prices differed 0-67%
  across 3 metros on one day (one snapshot; report differences, never motives). Verified stores: Cincinnati `01400513`,
  Los Angeles `70300022` (Ralphs Fresh Fare), Denver `62000115` (King Soopers Union Station); store format unknown
  except LA (fresh-fare).
- **Open Prices: supplementary.** US 47,111 prices (8,922 in last 90 days). Good for Safeway (~17.7k), Whole Foods
  (~6.0k), Target (~1.6k); negligible for Kroger; none for our 8 anchor UPCs. Data licence ODbL (attribute, share-alike).
- **Walmart: UNPROVEN.** Product page has price + `sellerType`; but PerimeterX "Robot or human?" fired on the 3rd request
  from the cloud IP, so the cloud session stopped (correct per rules). Sitemaps are indexes (6,873 children x 50,000
  URLs ~ 344M URLs) vs 17,280 pages/day at 5 s => a full pass is impossible. `/browse` allowed; `/search` and `/api/`
  disallowed by robots. Next step is the owner's Unraid run (below), nothing else from the cloud.
- **H-E-B:** page has price + GTIN in JSON-LD but only for a site-assigned default store. Store change = GraphQL
  `UpdatePreferredStore` on `/graphql`, which robots.txt disallows => not usable; at most a default-store spot check.
- **Whole Foods:** no price until a store is set; browser code uses `store-id`/`store` URL param and internal
  `/api/store-affinity` etc. One test with the default id changed nothing. Out of the collector; use Open Prices.
- **Target:** page has no price; price API host `redsky.target.com` robots.txt is `Disallow: /` => never called. Out.
- **Aldi:** robots `User-agent: *` Disallow `/` => out. **Albertsons/Safeway:** bot-challenge on first request => not worked around.
- **Neon over HTTP:** not checked. The cloud session has only an HTTPS proxy (no raw Postgres, no Docker daemon). Do not
  make the build depend on it.

## 6. What is NOT done / next steps (in recommended order)
**Owner answers after this handoff was first drafted:** Walmart run scheduled for 10 PM (owner's local time) on 2026-11-06; **next build step = Kroger collector**.
1. **Owner runs the Walmart seller check on Unraid** (scheduled 2026-11-06, 10 PM local) per `deploy/unraid/RUNBOOK.md` (build, pin gluetun by digest, egress
   check, kill-switch drill 7a/7b, then run **not before 2026-11-06 03:00 UTC**) and shares only the printed `summary`.
   Until then Walmart stays unproven. If the result is a block/challenge: Walmart is out (do not work around it).
2. **Next build step (owner chose it): Kroger collector.** BLS/FRED pages come after.
   - *Kroger collector* (GitHub Actions): read `basket.yaml` + `kroger_panel.yaml`; token refresh; batched product calls;
     change-only writes into `price_obs`; `collection_run` rows; self-cap at 50% of the daily limit; stop on 429;
     mock-server tests; populate `basket.yaml` (150 SKUs by grocery-spend share) and `product_group`s; need `DATABASE_URL`
     (Neon) as an Actions secret and KROGER_CLIENT_ID/SECRET. Re-validate panel stores monthly (closures); verify format
     of the other 12 candidate metros with the Locations API.
   - *Phase 1 gov data*: confirm every BLS series ID in the BLS Data Finder first (current ones are unverified guesses),
     then BLS/FRED clients, wedge and pass-through pages using `metrics.py`. Keys `BLS_API_KEY`, `FRED_API_KEY` exist in
     the cloud environment.
3. Phase 2 SEC scorecards (companyfacts XBRL; concept fallbacks; User-Agent required), brand->owner lookup.
4. Static site (Astro) + Cloudflare Pages + Worker; `/methodology` with every formula; `/stores` public status page.
5. Open Prices ingest; CA/UK/EU; lawyer consult before launch (legal posture in the plan: facts only, ToS risk is civil,
   not legal advice).
6. Neon-over-HTTP spike (optional).
- Possible Walmart discovery via `/browse` listing pages: only from the Unraid path, after cooldown, and only with the
  owner's go-ahead.

## 7. Mistakes made earlier (so you do not repeat them) and how they were caught
- Claimed Target's price API was covered by robots.txt after reading only `www.target.com`'s file; `redsky.target.com`
  is `Disallow: /`. Check robots on the host actually called.
- Claimed no site blocked crawlers; Aldi does (deep in the file). Use the real parser, not skimming.
- Open Prices first run reported zero coverage due to wrong filters and a fake server that shared the assumption;
  rebuilt from location records with self-checks. Do not trust a test whose fake server encodes your assumption.
- Tests that cannot fail (`or True`), mutation patterns that never applied, CI `| tee` masking failures (needs
  `pipefail`). Mutation-test new guard code and confirm each break fails a test.
- Overclaims caught by `jev_gate`: "image verified in session" (it was not; CI builds it), "no IP addresses anywhere"
  (tests use public example IPs). Say what was actually verified.
- Walmart optimism: the cloud IP was challenged on request 3; "collectable" was withdrawn.

## 8. Environment/tooling gotchas
- Cloud session: HTTPS proxy only; `gh pr create` is blocked (GraphQL) -> use `gh api repos/cost-bot-ai/cost-bot/pulls`.
  Push with `git push -u origin <branch>`; shallow clones need `git remote set-branches --add origin <branch>` + fetch.
  Amend + force-push was blocked by the permission classifier; add new commits instead.
- Network allowlist is changed only via the environment approval prompt (`update_environment`); remove temporary hosts
  after use. `DATABASE_URL` was removed from the cloud environment at the owner's request.
- Env var names present in the cloud env: `BLS_API_KEY`, `FRED_API_KEY`, `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET`
  (values never printed). `.env`/`.env.*`/`raw/` are git-ignored; the example file is `deploy/unraid/env.example`.
- Local throwaway Postgres 16 under `/var/tmp` is how schema tests were run in-session; remove afterwards.
- Unverified (not testable from the cloud): Docker build/run of the image beyond CI, gluetun log wording, WireGuard
  interface name, how the owner's Unraid provides Compose, gluetun's kill-switch (tested only by the runbook drill).
- Plan-mode toggling by the harness happens often; edits outside the plan file are blocked while it is on.

## 9. Open questions for the owner
Walmart run result (after 2026-11-06 10 PM local); which ~150 grocery SKUs (or accept a proposed basket);
Neon project/Actions secrets timing; domain name (~$10/yr, the only certain cost); lawyer consult timing.

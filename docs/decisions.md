# Decisions

Dated, owner-made decisions and what they changed. Facts and measurements live in `docs/spikes.md`.

## 2026-11-05: Walmart scope (owner chose "entire catalog")

- **Chosen by the project owner**, after being told the recommendation was a 1,000-2,000 product
  non-food basket and that full-catalog crawling was not recommended.
- **Measured afterwards:** the published sitemaps list on the order of hundreds of millions of product
  URLs (see `docs/spikes.md`). At the polite rate one client can read at most 17,280 pages a day, so a full
  pass is impossible by orders of magnitude. The decision has to be revisited with these numbers.
- **What stays fixed regardless:** `robots.txt` honored in code; identified User-Agent; no browser spoofing,
  proxy rotation or CAPTCHA solving; stop on the first block or challenge; facts only.
- **Status:** not implemented. Awaiting the owner's choice between a tiered plan (daily basket plus a
  slow rolling sample) and some other scope.

## 2026-11-05: Walmart, first-party items only (owner request)

- Limit Walmart to items Walmart itself sells (`sellerType == INTERNAL` on the product page); ignore marketplace
  listings. Verified that the flag exists and separates a first-party item from two marketplace ones.
- **Not yet feasible to scale:** the flag is only visible after reading each page; Walmart's bot protection
  challenged this session's IP on the third consecutive page. Discovery through category listing pages and a
  re-test from the Unraid path (after a 24 hour cooldown) are the next steps. Nothing is implemented.

## 2026-11-05: Target

- Pages carry no price; the price API host (`redsky.target.com`) disallows all crawling, so it is not used.
  Target is out of the automated collector. Open Prices is the main source for Target.

## 2026-11-05: Aldi

- `robots.txt` disallows our User-Agent from the whole site and points to its Terms of Service.
  Out of the automated collector.

## 2026-11-05: Schema 002, product equivalence (owner chose "schema change first")

- Each GTIN stays its own `product` row. A `product_group` says several GTINs are the same thing to a shopper, so prices
  compare across divisions (Kroger's private-label UPCs differ by division). Retailer item ids (Walmart `usItemId`,
  H-E-B sku) go in `product_external_id`.
- *Deviation from the plan:* the plan proposed a `product_alias(gtin, retailer, division)` table. Once each GTIN is its own
  row, `group_id` does that job; what was actually missing was a home for retailer ids.
- `price_obs.status` is `priced` or `unpriced`; "returned by the API but no price at this store" is recorded, not
  dropped and not stored as zero. A price can exist while the item is out of stock (`stock_level`).
- `store.format` and `store.division` default to NULL: unknown stays unknown.
- `scripts/migrate.py` records a checksum per file and refuses to continue if an applied file was edited.

## 2026-11-05: Unraid setup (owner chose "yes, prepare it")

- A Proton VPN container (gluetun, WireGuard) plus the sampler sharing its network, with a runbook. The owner must read
  Proton's terms on automated traffic first; that has not been checked.
- Settings for the VPN container were taken from the gluetun project's own wiki. Its "kill switch" is the project's claim
  (README), not something verified here, so the runbook tests it before any retailer traffic.
- `scripts/guarded_run.py` puts the rules in code: VPN egress must differ from the owner's home IP, a failed IP check means
  nothing else runs, 24 hours of refusal after a block or challenge, 1 hour minimum between runs, unreadable state fails
  closed, and no IP address is written to any output.
- Not verified from the cloud session (no Docker daemon): building and running the image. CI builds it and exercises the
  fail-closed behaviour with no network. Not verified at all: gluetun's log wording, the WireGuard interface name,
  and how the owner's Unraid version provides Docker Compose.

## 2026-11-05: Front-end study of H-E-B and Whole Foods (owner chose "study their front-end code")

- Done within the pre-agreed boundaries (static chunks only, robots.txt honoured, nothing disallowed or internal called).
- **H-E-B:** store selection is a GraphQL mutation on `/graphql`, which its robots.txt disallows. Not usable.
- **Whole Foods:** store comes from the browser code and internal `/api/...` endpoints; the server page ignored the one URL
  parameter tested. Not usable without internal APIs, which we do not call.
- Neither is a multi-store source. Whole Foods leaves the automated collector (Open Prices instead); H-E-B stays a
  single-store spot check.
- The host `cx.static.heb.com` was allowed temporarily for this study and removed afterwards.

## 2026-11-05: Next build step and Walmart run (owner)

- **Next build step: the Kroger collector** (before the BLS/FRED category pages).
- The owner scheduled the Walmart seller check on Unraid for 10 PM (their local time) on 2026-11-06, which is after the
  runbook's earliest time of 2026-11-06 03:00 UTC. Nothing Walmart-related runs from the cloud session.
- `docs/HANDOFF.md` added as the entry point for whoever continues the work.

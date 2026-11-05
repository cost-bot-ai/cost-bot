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

# Phase 0 spikes

## 1. Kroger public API (run 2026-11-05)

**Method.** `scripts/spike_kroger.py --term "large eggs" --metros cincinnati,los-angeles,denver`,
run three times (28 requests total, 2 s pacing, no 401/403/429). Credentials from the environment;
the output and raw payloads were checked for the client secret (not present). Raw payloads are
git-ignored under `raw/`. Extracted values were audited against the raw text with `jev_audit`
(12 values, 0 flagged). One snapshot, three stores: treat every price below as an observation,
not a trend.

### Answers

| Question | Result |
|---|---|
| Auth | OAuth2 client-credentials, scope `product.compact`, token `expires_in` = **1800 s** |
| Rate limits (from response headers) | **Products 10,000/day, Locations 5,000/day**, counted per endpoint (`x-ratelimit-limit-day`, `x-ratelimit-remaining-day`, `ratelimit-reset` ≈ 80,700, seconds until the daily window resets, unconfirmed). Matches the unofficial 10k figure; the "4 calls/day" catalog entry was wrong. |
| Does `filter.productId` batch? | **Yes.** 8 comma-separated UPCs in one call returned 8 products at all 3 stores. Maximum batch size is **untested** (8 verified; 25 requested next). |
| Location search | `filter.zipCode.near` + `filter.radiusInMiles` works. Fields: `locationId, chain, name, storeNumber, divisionNumber, address, geolocation, hours, departments, phone`. Banner codes seen: `KROGER, HART, RALPHS, FOOD4LESS, KINGSOOPERS`. |
| Price location | `data[].items[0].price.regular` (number, dollars); `.promo` never seen in the 47 priced items in the final run; `.effectiveDate` / `.expirationDate` present (useful for change detection) |
| Size | `items[0].size`, free text: `"12 ct"`, `"18 ct"`, `"12 ct / 24 oz"`. Needs a parser; mixed formats exist |
| Stock | `items[0].inventory.stockLevel`: `HIGH`, `TEMPORARILY_OUT_OF_STOCK`, or absent. A price can be present while out of stock |
| Pagination | term search: `meta.pagination.total` = 35, default `limit` = 10 |

### Data-quality findings (each needs handling in the collector)

1. **Non-retail nodes in Locations.** Cincinnati's first 5 results included 4 non-stores
   ("Harris Teeter - Unused Spoke", "Spoke Forecast", "Forecast Shed", all at 1014 Vine St,
   0 departments). Their names changed between runs. **Filter on `departments` being non-empty,
   not on names.**
2. **A returned product is not a priced product.** For 8 UPCs requested, Cincinnati returned
   8 products but only **2 had a price object**; LA and Denver priced all 8. Missing price means
   "not sold/priced here", never a price drop of 100%.
3. **Private-label UPCs differ by division.** Top-10 "large eggs" results: LA and Denver shared
   **8 of 10** UPCs; Cincinnati shared **0** with either. Some UPCs do span all three (two
   priced in Cincinnati). So `gtin` alone is not a cross-division product key: we need an
   equivalence mapping (brand + size + type) on top of it.
4. **Store format matters.** The LA store returned first was "Ralphs Fresh Fare", a different
   format from a standard Ralphs. Panel stores must record format, and we need 2 stores per
   metro before saying anything about a metro.

### Same UPC, same day, three stores (regular price, USD)

| UPC | Product | Cincinnati | Los Angeles | Denver | Spread (max/min − 1) |
|---|---|---|---|---|---|
| 0001111090406 | Kroger cage-free AA large white, 12 ct | n/p | 2.99 | 1.79 | 67% |
| 0001111008963 | Kroger cage-free AA large white, 18 ct | n/p | 4.39 | 2.65 | 66% |
| 0001111009039 | Kroger cage-free XL AA white, 12 ct | n/p | 3.19 | 1.99 | 60% |
| 0001111087374 | Simple Truth cage-free large brown, 18 ct | 5.49 | 6.99 | 5.49 | 27% |
| 0001111079769 | Simple Truth cage-free AA large brown, 12 ct | n/p | 4.99 | 3.99 | 25% |
| 0071514171682 | Eggland's Best organic large brown, 12 ct | 5.99 | 6.99 | 5.99 | 17% |
| 0001111005971 | Simple Truth Organic cage-free, 18 ct | n/p | 8.49 | 7.29 | 16% |
| 0071514151471 | Eggland's Best cage-free large white, 12 ct | n/p | 5.49 | 5.49 | 0% |

`n/p` = product returned with no price at that store. LA was higher than Denver on 7 of 8 and
equal on 1; where Cincinnati was priced it matched Denver both times.

**What this does and does not show.** It shows that identical UPCs carry materially different
shelf prices across Kroger-family stores on the same day, so a per-metro view is worth building.
It does **not** show why. Three stores, one product category, one day, one Fresh Fare-format
store, and eggs are volatile and subject to state-level production rules (not verified here).
Publish differences, not motives.

### Call budget (replaces the plan's estimate)

- Unbatched worst case: 150 SKUs × 40 stores = 6,000 products calls/day (60% of the limit).
- Batched at the verified 8 per call: 150 / 8 = 19 calls per store → **~760 calls/day for 40
  stores (7.6%)**. Larger batches are untested.
- Locations: negligible (panel is pinned; re-validate monthly).
- Plan stays: self-cap at 50% of the daily limit, stop on any 429, refresh the token before
  its 30-minute expiry.

### Still open

- Maximum batch size (try 25, 50).
- Whether `promo` appears at all, and how sale prices are represented.
- Behavior at the limit (a deliberate 429 was not provoked).
- Suburban store per metro; the three stores used are all downtown.
- Pagination for building the basket from term searches (35 results for "large eggs").

## 2. Target and Walmart (logged-out)

**Run 2026-11-05 from this cloud session (a datacenter IP).** `scripts/spike_retailers.py`:
product *pages* only, `robots.txt` fetched and enforced in code, identified User-Agent. We set
no cookies, no headers beyond User-Agent and `Accept`, no proxies and no TLS/browser fingerprint
(the HTTP client's default cookie jar does return any cookie the site itself sets), 5 s apart,
stop at the first block or challenge. The stop/robots/page-cap rules are covered by tests that
drive `run()` against a mock server; deliberately breaking each rule made those tests fail
(4 of 5 breaks caught; the fifth is behaviorally equivalent, since any non-200 is also blocked).
Requests: 2 `robots.txt`, 2 Target product pages, 1 Walmart product page (plus one plain
homepage request each earlier, to confirm the allowlist, and one `robots.txt` request to
`redsky.target.com`, which turned out to disallow everything). Nothing was blocked, challenged or
rate-limited. Extracted values audited against the page text with `jev_audit` (10 values, 0 flagged).

### robots.txt

| | Product pages | Excluded |
|---|---|---|
| Target | `/p/` allowed (only specific sections such as `/bp/p/` and `/p/premium-registry` are disallowed); publishes a product-page sitemap | `/s?` (search) |
| Walmart | `/ip/` not disallowed | `/api/…`, `/search`, `/store/…/search` and several `electrode/api` paths |
| `redsky.target.com` (Target's price API host) | n/a | **Everything**: `User-agent: *`, `Crawl-delay: 1`, `Disallow: /` (checked 2026-11-05) |

Consequences for the design: **never use search pages; never call Walmart's `/api/` paths;
never call `redsky.target.com`.**

*Correction:* an earlier version of this document (and the PR text) said Target's price API was
"not covered by its `robots.txt`". That was wrong: I had read only `www.target.com`'s file. The
API lives on a separate host whose own `robots.txt` disallows all crawling.

### Results

| | Walmart | Target |
|---|---|---|
| Page fetch | HTTP 200, 557 KB, no challenge | HTTP 200 (two pages, 349 KB and 367 KB), no challenge |
| Price in the page? | **Yes.** `__NEXT_DATA__` → `props.pageProps.initialData.data.product`: item 421705528, `priceInfo.currentPrice.price` = **1.67**, `unitPrice` "7.0 ¢/oz". The same $1.67 appears in the page text | **No.** `isProductDetailServerSideRenderPriceEnabled` is `False`; no price keys in the embedded JSON and no `$` price in the visible HTML |
| Where the price must come from | the page itself | a separate call after load; the page's config references the bare host `redsky.target.com` (no path, no parameters). The exact endpoint lives in JavaScript bundles that were **not fetched** |
| Structured data | one JSON-LD block, type `WebPage` only (no Product/offers) | none |
| Store used | `3081`, assigned automatically from our IP; we selected nothing | `2766`, assigned automatically |

Traps found:
- **The page contains other products' prices.** Walmart's embedded JSON includes an ad slot
  for a different item (`usItemId` 10449724, $5.96). Match on the page's own item ID; the first
  version of the spike script would have called Target "ok" with no price at all.
- **Location is chosen by the retailer from our IP**, not by us. Multi-metro tracking would need
  a way to select a store; how that works logged-out was **not tested**.

### What this does and does not show

- It shows Walmart's product-page HTML can carry the price for a single logged-out,
  identified, polite request, and that Target's cannot.
- It does **not** show that daily collection would keep working. One request from a datacenter IP
  is weak evidence either way; bot management here is volume/behaviour/IP-reputation based.
  The result also says nothing about the Unraid + Proton path, which is what the plan intends to
  use. Re-run there before relying on it.

### Decision: Target prices

Target's page has no price, and the API that does is on a host whose `robots.txt` disallows all
crawling. The project's rule is to honor `robots.txt`, so **no request was made to
`redsky.target.com`** (the allowed-hosts entry for it was also removed from the environment).
Target is therefore **out of the automated collector** for now. Overriding `robots.txt` would be a
separate, explicit decision by the project owner; it is not recommended: it removes the project's
clearest good-faith position if a retailer ever objects.

Ways to still cover Target without that: Open Prices contributions (receipt/price-tag photos),
and weekly-ad data. Unverified idea worth checking: Target's affiliate programme may offer an
official product feed that includes prices (needs approval; terms unknown).

### Additional retailers requested: robots.txt only (checked 2026-11-05)

Five more sites were added to the allowlist. Their `robots.txt` files were fetched first (5 requests); the
product-page results are in the next subsection.

*Correction:* I first wrote that none of the five blocked the whole site. That was wrong for Aldi: the
`User-agent: *` group is at line 564 of a 26-group file and I had searched with keywords. The page
client's real parser caught it and refused to fetch.

| Site | Whole-site block? | Product pages | Notable exclusions | Sitemaps |
|---|---|---|---|---|
| `www.wholefoodsmarket.com` | no | no product rule | `/locations/`, account, login, cart | 1 |
| `www.heb.com` | no | no product rule | `/search`, `/graphql`, `*/ajax/*`, `/category/*?*`, `/brand/*`, account, cart, loyalty | 1 index |
| `www.albertsons.com` | no (`Allow: /`) | no product rule | account, coupons/rewards, my list, customer account | 3 (`/shop/sitemaps/`) |
| `www.safeway.com` | no (`Allow: /`) | no product rule | same list as Albertsons | 3 |
| `www.aldi.us` | **yes, for us** | n/a | allows only named search/social bots; its `User-agent: *` group is `Disallow: /`; the file also says crawlers must follow its Terms of Service | 1 |

Rules for any later test: product pages only; never search pages, `/api/`, GraphQL or AJAX paths
(H-E-B and Aldi disallow them explicitly); discover pages through the published sitemaps. "Albertsons"
is a family: the same company runs the Albertsons and Safeway sites above plus others (Vons, Jewel-Osco,
Acme, Shaw's, Star Market, Tom Thumb, Randalls, Pavilions) whose sites were not checked. Whole Foods is
Amazon-owned, so its terms are likely stricter than its `robots.txt` suggests; not reviewed.

### Product-page tests on the added retailers (run 2026-11-05)

Same client and rules as before (product pages only, `robots.txt` enforced in code, identified
User-Agent, 5 s apart, stop at the first block or challenge, no retry). 5 `robots.txt` + 4 page
requests (Aldi was refused by the parser before any page request). These are single requests from a datacenter IP, so treat each as one observation.

| Site | Result | What it means |
|---|---|---|
| Aldi | **Not fetched.** `robots.txt` disallows our User-Agent from the whole site (both candidate URLs) | Out of the automated collector. Same treatment as Target's API host |
| Albertsons | HTTP 200 but a **"Pardon Our Interruption" bot-challenge page** (5,871 bytes). Stopped, no retry | Blocked on the first request. Cause unknown: datacenter IP, our non-browser User-Agent, or both |
| Safeway | Identical challenge page, same size | Same company and platform; same result |
| H-E-B | HTTP 200, 218 KB, real product page. `JSON-LD` Product with **price 2.66 USD, GTIN 041220201259**, in stock; page also lists other products' prices (2.86, 3.16, 3.46) and asks for a store; default store id 92 | **Best new candidate.** The GTIN lets it join Kroger and Open Prices rows. Price is tied to a store the site picked for us |
| Whole Foods | HTTP 200, 103 KB, real page. Product JSON-LD has a name but **no offer and no GTIN**; the page has 2 `$` characters in total; `isDefaultLocation: True`, empty location cookie, default store Lamar (TX) | **No price until a store is chosen.** Choosing one needs a location cookie or a click, untested |

Things this does and does not show:
- The "bot challenge" on Albertsons/Safeway is a single observation. Our User-Agent honestly says what we
  are; many bot-management systems challenge non-browser agents. We **do not** spoof a browser to get
  past it (that is the line the project holds). Whether a home or Proton IP behaves differently is untested.
- H-E-B is the only new site where a price was readable, and only for the store the site assigned.
- Prices at these sites depend on the store. A real collector needs a sanctioned way to choose it.

## Walmart catalog size (from its published sitemaps, 2026-11-05)

The decision was to aim at Walmart's **entire** inventory. The published sitemaps were measured first.

- `robots.txt` lists 32 sitemaps. Three look like item lists, and all three are **indexes**:
  `sitemap_itp_01.xml` → 100 child sitemaps, `sitemap_itp_02.xml` → 201, `sitemap_product_03.xml` → **6,873**.
- One child sampled from each of two indexes held exactly **50,000** product (`/ip/`) URLs.
- If every child is full, `sitemap_product_03` alone is about **344 million** URLs (an upper bound from
  one sample). The sampled URLs were long-tail listings (a wall poster, loafers, contact lenses), not groceries.
- Downloading all of it is itself heavy: the two sampled children were 2.7 MB and 2.2 MB compressed,
  so the product index alone is on the order of 18 GB.

Under the project's polite-crawler rule (one request every 5 s) a single client can load at most
**17,280 pages a day**. At that rate, 344 million URLs is about 20,000 days (over 50 years), and even the
smaller `itp` indexes (about 15 million URLs, an upper bound) take over two years for one pass. A daily
price for the full catalog is therefore not achievable by orders of magnitude, whatever the infrastructure.

## 3. Open Prices coverage (run 2026-11-05)

**Method.** `scripts/spike_openprices.py`: 21 read-only requests (budget 45), 2 s apart, identified
User-Agent, stops on 403/429/5xx. The host publishes no `robots.txt` (the path returns the website
shell). The API spec is at `/api/schema` (YAML). Retailer coverage is computed from the US
*location* records (392 stores, each with its OSM name and `price_count`), matched by name locally.

**A mistake worth recording.** My first version filtered prices server-side with
`location__osm_name__contains` and `currency=USD`, and reported **zero for every retailer**,
including Walmart and Whole Foods. That was wrong: the name filter returned 0 for a name known to
exist, and `currency=USD` includes non-US stores (the first "USD" result was in Melbourne). The unit
tests passed because they ran against a fake server written from the same wrong assumption. The
script now **self-checks each server-side filter against known data** and reports the result; all
four checks passed on this run (location counts sum to the country total; US store count matches;
date filter is a subset; `product_code__in` finds a known code).

### Size

| | |
|---|---|
| All prices | 340,509 across 128 countries (147,627 products with a price) |
| France / **US** / Norway / Germany | 218,538 / **47,111** / 25,045 / 17,644 |
| US stores | 392; US prices added in the last 90 days: **8,922** (~3,000 a month) |

### US coverage by retailer (stores matched by name; 90 days = created since 2026-08-07)

| Retailer | Stores | Prices all-time | Last 90 days |
|---|---|---|---|
| Safeway (an Albertsons banner) | 14 | 17,688 | 3,760 |
| Whole Foods | 20 | 5,967 | 1,284 |
| Target | 17 | 1,574 | 1,142 |
| Walmart | 40 | 646 | 74 |
| Aldi | 15 | 185 | 82 |
| Fred Meyer (Kroger) | 3 | 107 | 3 |
| H-E-B | 6 | 20 | 0 |
| Kroger | 7 | 4 | 1 |
| Albertsons (name match only) | 1 | 1 | 1 |
| Ralphs / Harris Teeter | 1 / 1 | 1 / 1 | 0 / 0 |
| King Soopers | 0 | 0 | n/a |

Also well covered but not on our list: Nob Hill Foods 4,054, Draeger's 1,859, Sprouts 1,803,
Smart & Final 1,614, Costco 1,488, Trader Joe's 891. Several of the top names are Northern
California chains, which suggests geographic and contributor concentration (not verified).

**Our 8 Kroger anchor UPCs have 0 crowd prices** (the code filter was verified to work).

### What this means

- **Open Prices cannot stand in for the Kroger API.** Kroger-family coverage is a few prices in
  total. Kroger stays an API-only source.
- **Target is the one retailer where Open Prices gives a usable signal** (1,574 prices, 1,142 in
  the last 90 days, from 17 stores), which matters now that Target is out of the automated collector.
  How many contributors produce that is unknown; a single contributor would make it fragile.
- **Whole Foods and Safeway are the best-covered chains**, but the data is thin and geographically
  clustered. Treat it as spot evidence, not a time series per store.
- **"Albertsons" is a family of banners.** Only Safeway and the literal name "Albertsons" were
  checked; Vons, Jewel-Osco, Acme, Shaw's, Star Market, Tom Thumb, Randalls and Pavilions were not.
- Crowd prices are sparse per product and per store; they suit "what did this cost at this store on
  this date" evidence with a receipt, not daily tracking.

### Not covered
Who the contributors are, price quality/moderation, and whether any given product has repeated
observations over time at one store.

## 4. Neon over HTTP

Not started. Neon's SQL-over-HTTP path is still to be checked (this session cannot reach Postgres
directly).

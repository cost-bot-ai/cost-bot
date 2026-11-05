-- 002: product equivalence, retailer item ids, store format/division, and "unpriced" observations.
-- Forward-only and non-destructive: existing rows keep their meaning (existing price_obs rows become 'priced').
--
-- Why (measured in docs/spikes.md):
--   * Kroger private-label UPCs differ by division (a Cincinnati store shared none of its top-10 egg UPCs with Los
--     Angeles or Denver). Each GTIN stays its own `product` row; a `product_group` says "these are the same thing
--     to a shopper" (same brand, size and type) so prices can be compared across divisions.
--   * A product can be returned by an API yet have no price at that store (Cincinnati: 6 of 8). That is not a price
--     drop, so it must be recordable as 'unpriced' rather than as a missing row or a zero.
--   * Walmart (usItemId) and H-E-B (sku) identify items by their own ids, and GTIN may be absent.

CREATE TABLE product_group (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,                       -- shopper-facing, e.g. 'Kroger cage-free AA large white eggs'
    brand_id    INTEGER REFERENCES brand(id),
    category_id INTEGER REFERENCES category(id),
    size_value  NUMERIC,
    size_unit   TEXT,
    notes       TEXT                                 -- why these GTINs were judged equivalent
);
CREATE INDEX product_group_name ON product_group (name);

ALTER TABLE product ADD COLUMN group_id INTEGER REFERENCES product_group(id);
CREATE INDEX product_group_members ON product (group_id);

-- A retailer's own id for a product (Walmart usItemId, H-E-B sku, Target tcin, Kroger productId).
CREATE TABLE product_external_id (
    id           SERIAL PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES product(id),
    retailer_id  INTEGER NOT NULL REFERENCES retailer(id),
    external_id  TEXT NOT NULL,
    UNIQUE (retailer_id, external_id)
);
CREATE INDEX product_external_id_product ON product_external_id (product_id);

ALTER TABLE store ADD COLUMN format   TEXT;   -- e.g. 'fresh-fare', 'standard'; NULL = unknown (do not guess)
ALTER TABLE store ADD COLUMN division TEXT;   -- retailer pricing division where known (Kroger divisionNumber)

-- Observations: 'priced' has a price; 'unpriced' means the product exists at the store but carries no price.
ALTER TABLE price_obs ADD COLUMN status TEXT NOT NULL DEFAULT 'priced'
    CHECK (status IN ('priced', 'unpriced'));
ALTER TABLE price_obs ADD COLUMN stock_level TEXT;     -- e.g. 'HIGH', 'TEMPORARILY_OUT_OF_STOCK'; a price can exist while out of stock
ALTER TABLE price_obs ALTER COLUMN price_cents DROP NOT NULL;
ALTER TABLE price_obs ADD CONSTRAINT price_obs_status_matches_price
    CHECK ((status = 'priced') = (price_cents IS NOT NULL));

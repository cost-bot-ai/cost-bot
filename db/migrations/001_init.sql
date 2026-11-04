-- PriceMemory core schema. Plain Postgres; no extensions required.
-- Facts only: price, size, store, date, identifiers. No retailer images/descriptions.

CREATE TABLE company (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    ticker      TEXT,
    cik         TEXT,                 -- SEC CIK, zero-padded to 10 digits
    country     TEXT NOT NULL DEFAULT 'US'
);

CREATE TABLE brand (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    owner_id    INTEGER REFERENCES company(id),
    source      TEXT                  -- where the ownership claim came from (10-K, OFF, wiki...)
);

CREATE TABLE category (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE, -- e.g. 'eggs'
    name        TEXT NOT NULL
);

CREATE TABLE product (
    id          SERIAL PRIMARY KEY,
    gtin        TEXT UNIQUE,          -- 14-digit normalized GTIN/UPC; join key across sources
    name        TEXT NOT NULL,
    brand_id    INTEGER REFERENCES brand(id),
    category_id INTEGER REFERENCES category(id),
    size_value  NUMERIC,              -- normalized later to oz / ct / L in unit_price calc
    size_unit   TEXT
);

CREATE TABLE retailer (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE, -- 'kroger', 'target', 'walmart', 'openprices'
    name        TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE   -- kill switch
);

CREATE TABLE store (
    id           SERIAL PRIMARY KEY,
    retailer_id  INTEGER NOT NULL REFERENCES retailer(id),
    external_id  TEXT NOT NULL,       -- e.g. Kroger locationId
    banner       TEXT,                -- 'Ralphs', 'Harris Teeter', ...
    metro        TEXT,                -- our panel label, e.g. 'los-angeles'
    zip          TEXT,
    lat          NUMERIC,
    lon          NUMERIC,
    UNIQUE (retailer_id, external_id)
);

-- Change-only price observations. Insert when price/promo/size changes, plus a weekly heartbeat.
CREATE TABLE price_obs (
    id           BIGSERIAL PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES product(id),
    store_id     INTEGER NOT NULL REFERENCES store(id),
    observed_at  TIMESTAMPTZ NOT NULL,
    price_cents  INTEGER NOT NULL CHECK (price_cents >= 0),
    promo_cents  INTEGER CHECK (promo_cents >= 0),   -- NULL when no promo
    size_value   NUMERIC,
    size_unit    TEXT,
    unit_price   NUMERIC,             -- price per normalized unit, computed at write time
    source       TEXT NOT NULL        -- 'kroger_api', 'target_web', 'openprices', ...
);
CREATE INDEX price_obs_lookup ON price_obs (product_id, store_id, observed_at DESC);

-- One row per collector run; powers the public /stores transparency page.
CREATE TABLE collection_run (
    id           BIGSERIAL PRIMARY KEY,
    retailer_id  INTEGER NOT NULL REFERENCES retailer(id),
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL,       -- ok | partial | blocked | rate_limited | error
    requests     INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    http_errors  INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

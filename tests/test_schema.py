"""Schema + migration tests against a REAL Postgres (set TEST_DATABASE_URL; skipped otherwise).

Each test runs in its own throwaway schema, so tests never touch each other or real data.
"""
import importlib.util
import os
import pathlib
import shutil
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
_spec = importlib.util.spec_from_file_location("migrate", ROOT / "scripts" / "migrate.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


@pytest.fixture
def conn():
    name = "t_" + uuid.uuid4().hex[:12]
    c = psycopg.connect(DSN, autocommit=True)
    c.execute(f"CREATE SCHEMA {name}")
    c.execute(f"SET search_path TO {name}")
    yield c
    c.execute(f"DROP SCHEMA {name} CASCADE")
    c.close()


def only_001(tmp_path):
    d = tmp_path / "m001"
    d.mkdir()
    shutil.copy(MIGRATIONS / "001_init.sql", d)
    return d


def tables(c):
    return {r[0] for r in c.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}


def columns(c, table):
    return {r[0]: r[1] for r in c.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s", (table,))}


def seed(c):
    """Minimal graph used by several tests: one retailer, three stores, a brand and a category."""
    c.execute("INSERT INTO retailer (slug, name) VALUES ('kroger', 'Kroger')")
    c.execute("INSERT INTO brand (name) VALUES ('Kroger')")
    c.execute("INSERT INTO category (slug, name) VALUES ('eggs', 'Eggs')")
    for ext, metro in (("70300022", "los-angeles"), ("62000115", "denver"), ("01400513", "cincinnati")):
        c.execute("INSERT INTO store (retailer_id, external_id, metro) VALUES (1, %s, %s)", (ext, metro))


# ---- runner -----------------------------------------------------------------------------------

def test_fresh_database_gets_every_migration_in_order(conn):
    assert mig.migrate(conn) == ["001_init.sql", "002_product_equivalence.sql"]
    assert {"product_group", "product_external_id", "schema_migrations", "price_obs"} <= tables(conn)


def test_running_twice_applies_nothing_the_second_time(conn):
    mig.migrate(conn)
    assert mig.migrate(conn) == []


def test_editing_an_applied_migration_is_refused(conn, tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    shutil.copy(MIGRATIONS / "001_init.sql", d)
    mig.migrate(conn, d)
    (d / "001_init.sql").write_text((d / "001_init.sql").read_text() + "\n-- sneaky edit\n")
    with pytest.raises(mig.MigrationError, match="edited after it was applied"):
        mig.migrate(conn, d)


def test_a_failing_migration_leaves_nothing_half_applied(conn, tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "001_ok.sql").write_text("CREATE TABLE ok_table (id int);")
    (d / "002_bad.sql").write_text("CREATE TABLE half_table (id int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        mig.migrate(conn, d)
    assert "ok_table" in tables(conn) and "half_table" not in tables(conn)
    assert [r[0] for r in conn.execute("SELECT filename FROM schema_migrations")] == ["001_ok.sql"]


def test_a_migration_is_not_applied_if_it_cannot_be_recorded(conn, tmp_path):
    """The gap between 'ran the SQL' and 'wrote the bookkeeping row' must be atomic."""
    conn.execute("CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                 "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), CHECK (filename <> '001_unrecordable.sql'))")
    d = tmp_path / "m"
    d.mkdir()
    (d / "001_unrecordable.sql").write_text("CREATE TABLE should_not_exist (id int);")
    with pytest.raises(psycopg.errors.CheckViolation):
        mig.migrate(conn, d)
    assert "should_not_exist" not in tables(conn)           # rolled back together with the failed bookkeeping row


def test_empty_directory_is_an_error(conn, tmp_path):
    with pytest.raises(mig.MigrationError):
        mig.migrate(conn, tmp_path)


# ---- 002 on existing data ---------------------------------------------------------------------

def test_002_preserves_existing_rows_and_marks_them_priced(conn, tmp_path):
    mig.migrate(conn, only_001(tmp_path))
    seed(conn)
    conn.execute("INSERT INTO product (gtin, name) VALUES ('0001111090406', 'Kroger CF AA large white 12ct')")
    conn.execute("INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, source) "
                 "VALUES (1, 1, now(), 299, 'kroger_api')")
    assert mig.migrate(conn) == ["002_product_equivalence.sql"]       # only the new one runs
    row = conn.execute("SELECT price_cents, status, stock_level FROM price_obs").fetchone()
    assert row == (299, "priced", None)
    assert conn.execute("SELECT count(*) FROM product").fetchone()[0] == 1


# ---- 002 behaviour: the cases that motivated it ------------------------------------------------

def test_division_specific_upcs_group_into_one_comparable_product(conn):
    """The real egg case: LA and Denver share a UPC; Cincinnati's equivalent carton has a different one."""
    mig.migrate(conn)
    seed(conn)
    conn.execute("INSERT INTO product_group (name, brand_id, category_id, size_value, size_unit, notes) "
                 "VALUES ('Kroger cage-free AA large white eggs', 1, 1, 12, 'ct', 'same carton, division UPCs')")
    conn.execute("INSERT INTO product (gtin, name, group_id) VALUES ('0001111090406', 'west UPC', 1)")
    conn.execute("INSERT INTO product (gtin, name, group_id) VALUES ('0001111060903', 'central UPC', 1)")
    obs = [(1, 1, 299), (1, 2, 179), (2, 3, 189)]                       # (product, store, cents)
    for p, s, cents in obs:
        conn.execute("INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, source) "
                     "VALUES (%s, %s, now(), %s, 'kroger_api')", (p, s, cents))
    rows = conn.execute(
        "SELECT st.metro, o.price_cents FROM price_obs o JOIN product p ON p.id = o.product_id "
        "JOIN store st ON st.id = o.store_id WHERE p.group_id = 1 ORDER BY st.metro").fetchall()
    assert rows == [("cincinnati", 189), ("denver", 179), ("los-angeles", 299)]    # three metros, one product group


def test_unpriced_observation_is_recordable_and_is_not_a_zero(conn):
    mig.migrate(conn)
    seed(conn)
    conn.execute("INSERT INTO product (gtin, name) VALUES ('0001111009039', 'x')")
    conn.execute("INSERT INTO price_obs (product_id, store_id, observed_at, status, source) "
                 "VALUES (1, 3, now(), 'unpriced', 'kroger_api')")
    assert conn.execute("SELECT price_cents, status FROM price_obs").fetchone() == (None, "unpriced")


@pytest.mark.parametrize("sql", [
    "INSERT INTO price_obs (product_id, store_id, observed_at, status, source) VALUES (1, 1, now(), 'priced', 'x')",
    "INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, status, source) "
    "VALUES (1, 1, now(), 199, 'unpriced', 'x')",
    "INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, source) VALUES (1, 1, now(), -5, 'x')",
    "INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, status, source) "
    "VALUES (1, 1, now(), 199, 'free', 'x')",
])
def test_inconsistent_observations_are_rejected(conn, sql):
    mig.migrate(conn)
    seed(conn)
    conn.execute("INSERT INTO product (gtin, name) VALUES ('1', 'x')")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(sql)


def test_price_while_out_of_stock_is_kept_with_its_stock_level(conn):
    mig.migrate(conn)
    seed(conn)
    conn.execute("INSERT INTO product (gtin, name) VALUES ('1', 'x')")
    conn.execute("INSERT INTO price_obs (product_id, store_id, observed_at, price_cents, stock_level, source) "
                 "VALUES (1, 1, now(), 549, 'TEMPORARILY_OUT_OF_STOCK', 'kroger_api')")
    assert conn.execute("SELECT price_cents, status, stock_level FROM price_obs").fetchone() == (
        549, "priced", "TEMPORARILY_OUT_OF_STOCK")


def test_retailer_item_ids_are_unique_per_retailer_but_may_repeat_across_retailers(conn):
    mig.migrate(conn)
    seed(conn)
    conn.execute("INSERT INTO retailer (slug, name) VALUES ('walmart', 'Walmart')")
    conn.execute("INSERT INTO product (gtin, name) VALUES ('1', 'a')")
    conn.execute("INSERT INTO product (gtin, name) VALUES ('2', 'b')")
    conn.execute("INSERT INTO product_external_id (product_id, retailer_id, external_id) VALUES (1, 2, '421705528')")
    conn.execute("INSERT INTO product_external_id (product_id, retailer_id, external_id) VALUES (2, 1, '421705528')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO product_external_id (product_id, retailer_id, external_id) VALUES (2, 2, '421705528')")


def test_store_format_and_division_default_to_unknown_not_a_guess(conn):
    mig.migrate(conn)
    seed(conn)
    assert conn.execute("SELECT format, division FROM store WHERE metro = 'los-angeles'").fetchone() == (None, None)
    conn.execute("UPDATE store SET format = 'fresh-fare' WHERE metro = 'los-angeles'")
    assert conn.execute("SELECT format FROM store WHERE metro = 'los-angeles'").fetchone() == ("fresh-fare",)


def test_price_cents_is_now_nullable_but_nothing_else_loosened(conn):
    mig.migrate(conn)
    cols = columns(conn, "price_obs")
    assert cols["price_cents"] == "YES" and cols["status"] == "NO" and cols["observed_at"] == "NO"
    assert cols["product_id"] == "NO" and cols["store_id"] == "NO" and cols["source"] == "NO"

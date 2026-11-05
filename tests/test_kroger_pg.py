"""The Kroger collector against a REAL Postgres (set TEST_DATABASE_URL; skipped otherwise).

Each test gets its own throwaway schema with migrations 001 and 002 applied, and a fake Kroger API (no network).
"""
import importlib.util
import os
import pathlib
import shutil
import uuid
from datetime import timedelta

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

from collectors import kroger as k  # noqa: E402
from test_kroger import FakeApi, T0, basket_of, catalog_for, make_client, panel_of, product, upcs  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("migrate", ROOT / "scripts" / "migrate.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


@pytest.fixture
def schema():
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    admin.execute(f"SET search_path TO {name}")
    mig.migrate(admin, ROOT / "db" / "migrations")
    yield name
    admin.execute(f"DROP SCHEMA {name} CASCADE")
    admin.close()


@pytest.fixture
def conn(schema):
    c = psycopg.connect(DSN, options=f"-c search_path={schema}")      # autocommit off, like production
    yield c
    c.close()


def collect(conn, api, n=3, locs=("A",), when=T0, **kw):
    client, _ = make_client(api)
    return k.run(basket_of(n), panel_of(*locs), client, k.PgRepo(conn), now=lambda: when, **kw)


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def test_first_run_creates_retailer_store_products_and_observations(conn):
    res = collect(conn, FakeApi(catalog_for(["A"], 3)))
    assert res["status"] == "ok" and res["rows_written"] == 3
    assert q(conn, "SELECT slug, enabled FROM retailer") == [("kroger", True)]
    assert q(conn, "SELECT external_id, banner, metro, format FROM store") == [("A", "KROGER", "metro-A", None)]
    assert q(conn, "SELECT gtin FROM product ORDER BY gtin") == [(f"{i:014d}",) for i in (1, 2, 3)]
    assert q(conn, "SELECT count(*) FROM product_external_id")[0][0] == 3
    status, price, source = q(conn, "SELECT status, price_cents, source FROM price_obs ORDER BY id LIMIT 1")[0]
    assert (status, price, source) == ("priced", 100, "kroger_api")
    unit, size_unit = q(conn, "SELECT unit_price, size_unit FROM price_obs ORDER BY id LIMIT 1")[0]
    assert size_unit == "ct" and float(unit) == pytest.approx(100 / 12)
    run = q(conn, "SELECT status, requests, rows_written, http_errors, finished_at IS NOT NULL FROM collection_run")
    assert run == [("ok", 2, 3, 0, True)]


def test_rerun_writes_nothing_then_only_the_change_then_the_weekly_heartbeat(conn):
    cat = catalog_for(["A"], 3)
    collect(conn, FakeApi(cat))
    again = collect(conn, FakeApi(cat), when=T0 + timedelta(days=1))
    assert again["rows_written"] == 0 and again["unchanged"] == 3
    cat["A"][upcs(3)[0]] = product(upc=upcs(3)[0], regular=5.55, pid="idx")
    changed = collect(conn, FakeApi(cat), when=T0 + timedelta(days=2))
    assert changed["rows_written"] == 1
    assert q(conn, "SELECT price_cents FROM price_obs WHERE observed_at = %s", (T0 + timedelta(days=2),)) == [(555,)]
    beat = collect(conn, FakeApi(cat), when=T0 + timedelta(days=9))
    assert beat["rows_written"] == 3                       # none changed since day 2, but 7+ days have passed
    assert q(conn, "SELECT count(*) FROM product")[0][0] == 3 and q(conn, "SELECT count(*) FROM store")[0][0] == 1


def test_history_is_tracked_per_store(conn):
    cat = catalog_for(["A", "B"], 2)
    first = collect(conn, FakeApi(cat), n=2, locs=("A", "B"))
    assert first["rows_written"] == 4
    same = collect(conn, FakeApi(cat), n=2, locs=("A", "B"), when=T0 + timedelta(days=1))
    assert same["rows_written"] == 0
    cat["B"][upcs(2)[0]] = product(upc=upcs(2)[0], regular=7.77, pid="idb")
    changed = collect(conn, FakeApi(cat), n=2, locs=("A", "B"), when=T0 + timedelta(days=2))
    assert changed["rows_written"] == 1
    assert q(conn, "SELECT s.external_id, o.price_cents FROM price_obs o JOIN store s ON s.id = o.store_id "
                   "WHERE o.observed_at = %s", (T0 + timedelta(days=2),)) == [("B", 777)]


def test_unpriced_products_are_stored_as_unpriced_with_no_price(conn):
    cat = catalog_for(["A"], 2)
    cat["A"][upcs(2)[0]] = product(upc=upcs(2)[0], regular=None)
    collect(conn, FakeApi(cat), n=2)
    assert q(conn, "SELECT status, price_cents FROM price_obs ORDER BY status") == [("priced", 101), ("unpriced", None)]


def test_kill_switch_in_the_database_stops_everything(conn):
    collect(conn, FakeApi(catalog_for(["A"], 1)), n=1)
    conn.execute("UPDATE retailer SET enabled = false WHERE slug = 'kroger'")
    conn.commit()
    api = FakeApi(catalog_for(["A"], 1))
    res = collect(conn, api, n=1, when=T0 + timedelta(days=1))
    assert res["status"] == "disabled" and api.calls == []
    assert q(conn, "SELECT count(*) FROM collection_run")[0][0] == 1       # no new run recorded


def test_a_rate_limit_is_recorded_in_collection_run(conn):
    api = FakeApi(catalog_for(["A"], 16), fail={3: 429})
    res = collect(conn, api, n=16)
    assert res["status"] == "rate_limited"
    assert q(conn, "SELECT status, http_errors FROM collection_run") == [("rate_limited", 1)]
    assert q(conn, "SELECT count(*) FROM price_obs")[0][0] == 8            # the first batch was kept


def test_a_database_error_mid_run_still_closes_the_run_as_error(conn):
    class Broken(k.PgRepo):
        def write(self, *a, **kw):
            self._one("SELECT 1/0")

    client, _ = make_client(FakeApi(catalog_for(["A"], 1)))
    with pytest.raises(psycopg.errors.DivisionByZero):
        k.run(basket_of(1), panel_of("A"), client, Broken(conn), now=lambda: T0)
    assert q(conn, "SELECT status, finished_at IS NOT NULL FROM collection_run") == [("error", True)]


def test_an_unmigrated_schema_is_refused_before_anything_is_requested():
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        admin.execute(f"CREATE SCHEMA {name}")
        admin.execute(f"SET search_path TO {name}")
        only_001 = ROOT / "db" / "migrations" / "001_init.sql"
        admin.execute(only_001.read_text())
        api = FakeApi(catalog_for(["A"], 1))
        with pytest.raises(SystemExit, match="002"):
            client, _ = make_client(api)
            k.run(basket_of(1), panel_of("A"), client, k.PgRepo(admin), now=lambda: T0)
        assert api.calls == []
    finally:
        admin.execute(f"DROP SCHEMA {name} CASCADE")
        admin.close()

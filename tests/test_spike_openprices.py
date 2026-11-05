"""Open Prices is volunteer-run, so 'stop, don't retry, stay small' is tested; so are the self-checks
that stop us reporting a silent zero when a server-side filter does not behave."""
import importlib.util
import pathlib

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "spike_openprices", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "spike_openprices.py")
op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(op)


@pytest.fixture(autouse=True)
def _record_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(op.time, "sleep", lambda s: slept.append(s))
    return slept


def client_for(handler, budget=45):
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": op.UA})
    return op.Client(http, delay=2.0, budget=budget)


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_stops_on_block_or_server_error_without_retry(status):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(status, json={})

    c = client_for(handler)
    with pytest.raises(op.Stop) as e:
        c.get("/stats")
    assert len(seen) == 1 and str(status) in str(e.value) and "no retry" in str(e.value)


def test_paces_requests_after_the_first(_record_sleep):
    c = client_for(lambda req: httpx.Response(200, json={"total": 1}))
    c.get("/a"); c.get("/b"); c.get("/c")
    assert _record_sleep == [2.0, 2.0]


def test_request_budget_is_enforced():
    c = client_for(lambda req: httpx.Response(200, json={}), budget=2)
    c.get("/a"); c.get("/b")
    with pytest.raises(op.Stop) as e:
        c.get("/c")
    assert "budget" in str(e.value) and c.used == 2


@pytest.mark.parametrize("name,variants,expected", [
    ("Walmart Supercenter", ["walmart"], True),
    ("WALMART", ["walmart"], True),
    ("Whole Foods Market", ["whole foods"], True),
    ("H-E-B", ["h-e-b", "heb "], True),
    ("HEB Plus", ["h-e-b", "heb "], True),
    ("Hebrew Book Store", ["h-e-b", "heb "], False),   # the trailing space guards against this
    ("Giant", ["target"], False),
])
def test_retailer_name_matching(name, variants, expected):
    assert op.matches(name, variants) is expected


LOCS = [{"id": 1, "osm_name": "Walmart Supercenter", "price_count": 60},
        {"id": 2, "osm_name": "Whole Foods Market", "price_count": 30},
        {"id": 3, "osm_name": "Giant", "price_count": 10}]


def fake_api(product_filter_works=True, date_filter_works=True):
    def handler(req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, req.url.params
        if path.endswith("/stats"):
            return httpx.Response(200, json={"price_count": 1000})
        if path.endswith("/locations/osm/countries"):
            return httpx.Response(200, json=[
                {"country_code_2": "FR", "name": "France", "location_count": 9, "price_count": 700},
                {"country_code_2": "US", "name": "United States", "location_count": 3, "price_count": 100}])
        if path.endswith("/locations"):
            return httpx.Response(200, json={"items": LOCS, "page": 1, "pages": 1, "size": 100, "total": 3})
        if path.endswith("/prices"):
            if "product_code__in" in q:
                n = 1 if product_filter_works else 0
                return httpx.Response(200, json={"total": n, "items": [] if not n else [{
                    "product": {"code": "123"}, "price": 2.99, "currency": "USD", "date": "2026-10-01",
                    "location": {"osm_name": "Walmart Supercenter"}}]})
            if "created__gte" in q:
                return httpx.Response(200, json={"total": 5 if date_filter_works else 9999, "items": []})
            return httpx.Response(200, json={"total": 100, "items": [{"product": {"code": "123"}}]})
        return httpx.Response(404)
    return handler


def test_collect_counts_retailers_from_location_records_and_passes_self_checks():
    out = op.collect(client_for(fake_api()), ["0001111090406"], since="2026-08-01")
    assert out["retailers"]["Walmart"]["locations"] == 1 and out["retailers"]["Walmart"]["prices_all_time"] == 60
    assert out["retailers"]["Whole Foods"]["prices_all_time"] == 30
    assert out["retailers"]["Target"] == {"locations": 0, "prices_all_time": 0}   # absent => no 'recent' query
    assert out["top_us_store_names"][0] == ("Walmart Supercenter", 60)
    assert out["checks"]["location_price_counts_sum_matches_country_total"] is True
    assert out["all_checks_passed"] is True and out["anchor_upcs"]["trustworthy"] is True


def test_self_check_flags_a_server_filter_that_finds_nothing():
    out = op.collect(client_for(fake_api(product_filter_works=False)), ["0001111090406"], since="2026-08-01")
    assert out["checks"]["product_code_in_finds_a_known_code"] is False
    assert out["anchor_upcs"]["trustworthy"] is False and out["all_checks_passed"] is False


def test_self_check_flags_a_date_filter_that_returns_too_much():
    out = op.collect(client_for(fake_api(date_filter_works=False)), [], since="2026-08-01")
    assert out["checks"]["date_filter_is_a_subset"] is False and out["all_checks_passed"] is False


def test_planned_requests_fit_the_budget():
    c = client_for(fake_api())
    op.collect(c, ["0001111090406"], since="2026-08-01")
    assert c.used <= op.MAX_REQUESTS

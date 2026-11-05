"""Kroger collector: parsing, change-only storage, the API client's stop rules, and whole runs against a fake API.

The fake API is a MockTransport. Its payload shapes come from the measured spike (docs/spikes.md section 1):
products at data[].items[0].price.regular, size as free text, stock under items[0].inventory.stockLevel,
rate limits in x-ratelimit-*-day headers. No test calls the real API.
"""
import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from collectors import kroger as k

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
SECRET = "s3cret-value"
TOKEN = "tok-abc123"


# ---- parsing -----------------------------------------------------------------------------------

def test_gtin_is_digits_only_and_padded_to_14():
    assert k.normalize_gtin("0001111090406") == "00001111090406"
    assert k.normalize_gtin(" 0071514171682 ") == "00071514171682"
    for bad in ("", "abc", "12-34", "1234567", "123456789012345"):
        with pytest.raises(ValueError):
            k.normalize_gtin(bad)


def test_api_upc_is_the_13_digit_form_the_spike_verified():
    assert k.api_upc("00001111090406") == "0001111090406"
    assert k.api_upc("10012345678902") == "10012345678902"      # a real GTIN-14 keeps its leading 1


@pytest.mark.parametrize("text,expected", [
    ("12 ct", (12.0, "ct")), ("18 ct", (18.0, "ct")), ("12 ct / 24 oz", (12.0, "ct")),
    ("16 fl oz", (16.0, "fl oz")), ("1.5 L", (1.5, "l")), ("1 gal", (1.0, "gal")), ("2 lb", (2.0, "lb")),
    ("1 FL. OZ", (1.0, "fl oz")),
])
def test_parse_size(text, expected):
    assert k.parse_size(text) == expected


@pytest.mark.parametrize("text", [None, "", "family size", "ct 12"])
def test_parse_size_gives_none_when_there_is_no_size(text):
    assert k.parse_size(text) is None


def test_dollars_to_cents_rounds_half_up_and_rejects_junk():
    assert k.to_cents(2.99) == 299 and k.to_cents(0) == 0 and k.to_cents("4.395") == 440
    assert k.to_cents(1.005) == 101          # via Decimal(str(x)), not binary floating point
    assert k.to_cents(None) is None
    for bad in (-1, "free", float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            k.to_cents(bad)


def product(upc="0001111090406", regular=2.99, promo=None, size="12 ct", stock="HIGH", pid="p1", **extra):
    price = {}
    if regular is not None:
        price["regular"] = regular
    if promo is not None:
        price["promo"] = promo
    item = {"size": size, "inventory": {"stockLevel": stock} if stock else {}}
    if price:
        item["price"] = price
    return {"productId": pid, "upc": upc, "description": "Kroger Large Eggs", "brand": "Kroger",
            "items": [item], **extra}


def test_priced_product_becomes_a_priced_observation_with_unit_price():
    o = k.observation_from_product(product(), "62000115", T0)
    assert (o.gtin, o.status, o.price_cents, o.promo_cents) == ("00001111090406", "priced", 299, None)
    assert (o.size_value, o.size_unit, o.stock_level, o.location_id) == (12.0, "ct", "HIGH", "62000115")
    assert o.unit_price == pytest.approx(299 / 12)
    assert o.external_id == "p1" and o.brand == "Kroger"


def test_promo_is_kept_next_to_the_regular_price():
    o = k.observation_from_product(product(regular=3.99, promo=2.99), "x", T0)
    assert (o.price_cents, o.promo_cents) == (399, 299)


def test_product_without_a_price_is_unpriced_not_zero():
    for p in (product(regular=None), {**product(), "items": []}):
        o = k.observation_from_product(p, "01400513", T0)
        assert o.status == "unpriced" and o.price_cents is None and o.promo_cents is None and o.unit_price is None


def test_out_of_stock_item_keeps_its_price():
    o = k.observation_from_product(product(stock="TEMPORARILY_OUT_OF_STOCK"), "x", T0)
    assert o.status == "priced" and o.price_cents == 299 and o.stock_level == "TEMPORARILY_OUT_OF_STOCK"


def test_unparseable_size_still_records_the_price_without_a_unit_price():
    o = k.observation_from_product(product(size="family size"), "x", T0)
    assert o.price_cents == 299 and o.size_value is None and o.unit_price is None


def test_unusable_payloads_raise():
    with pytest.raises(KeyError):
        k.observation_from_product({"items": []}, "x", T0)             # no upc
    with pytest.raises(ValueError):
        k.observation_from_product(product(regular=-5), "x", T0)


# ---- change-only storage -----------------------------------------------------------------------

def prev_of(o, age=timedelta(days=1)):
    return k.Previous(o.observed_at - age, o.status, o.price_cents, o.promo_cents, o.size_value, o.size_unit)


def obs(**kw):
    return k.observation_from_product(product(**kw), "x", T0)


def test_first_sight_is_written():
    assert k.should_write(None, obs())


def test_unchanged_is_not_written():
    o = obs()
    assert not k.should_write(prev_of(o), o)


def test_stock_change_alone_is_not_written():
    assert not k.should_write(prev_of(obs(stock="HIGH")), obs(stock="TEMPORARILY_OUT_OF_STOCK"))


@pytest.mark.parametrize("change", [dict(regular=3.19), dict(promo=1.99), dict(size="18 ct"), dict(regular=None)])
def test_anything_a_shopper_would_notice_is_written(change):
    assert k.should_write(prev_of(obs()), obs(**change))


def test_unpriced_to_priced_and_back_are_both_written():
    unpriced, priced = obs(regular=None), obs()
    assert k.should_write(prev_of(unpriced), priced) and k.should_write(prev_of(priced), unpriced)


def test_unpriced_staying_unpriced_is_not_written_until_the_heartbeat():
    o = obs(regular=None)
    assert not k.should_write(prev_of(o), o)
    assert k.should_write(prev_of(o, age=k.HEARTBEAT), o)


def test_heartbeat_boundary():
    o = obs()
    assert not k.should_write(prev_of(o, age=k.HEARTBEAT - timedelta(seconds=1)), o)
    assert k.should_write(prev_of(o, age=k.HEARTBEAT), o)


# ---- configuration files -----------------------------------------------------------------------

def test_load_basket_normalizes_and_rejects_duplicates_and_bad_gtins(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(yaml.safe_dump({"products": [{"gtin": "0001111090406", "name": "Eggs", "category": "eggs"}]}))
    assert k.load_basket(f) == [k.BasketItem("00001111090406", "Eggs", "eggs")]
    f.write_text(yaml.safe_dump({"products": [{"gtin": "0001111090406"}, {"gtin": "1111090406"}]}))
    with pytest.raises(ValueError, match="duplicate"):
        k.load_basket(f)
    f.write_text(yaml.safe_dump({"products": [{"gtin": "eggs"}]}))
    with pytest.raises(ValueError):
        k.load_basket(f)
    f.write_text("products:\n  []\n")
    assert k.load_basket(f) == []


def test_load_panel_reads_pinned_stores_and_keeps_unknown_format_unknown(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump({"metros": [], "stores": [
        {"metro": "denver", "location_id": "62000115", "banner": "KINGSOOPERS", "name": "KS", "format": "unknown"},
        {"metro": "los-angeles", "location_id": "70300022", "banner": "RALPHS", "name": "R", "format": "fresh-fare"}]}))
    s = k.load_panel(f)
    assert [x.location_id for x in s] == ["62000115", "70300022"] and s[0].format is None and s[1].format == "fresh-fare"
    f.write_text(yaml.safe_dump({"stores": [{"metro": "a", "location_id": "1"}, {"metro": "b", "location_id": "1"}]}))
    with pytest.raises(ValueError, match="duplicate"):
        k.load_panel(f)


def test_the_committed_basket_and_panel_load():
    basket = k.load_basket(ROOT / "data/curated/basket.yaml")
    panel = k.load_panel(ROOT / "data/curated/kroger_panel.yaml")
    assert len(basket) >= 1 and len({b.gtin for b in basket}) == len(basket)
    assert len(panel) >= 1 and all(s.location_id for s in panel)


# ---- a fake Kroger API -------------------------------------------------------------------------

class FakeApi:
    """Records every request. `catalog[location][upc13]` is the product payload (or None = not returned)."""

    def __init__(self, catalog, limit=10000, remaining=10000, token_ttl=1800, fail=None):
        self.catalog, self.limit, self.remaining, self.token_ttl = catalog, limit, remaining, token_ttl
        self.fail = fail or {}                     # request number -> status code
        self.calls, self.tokens_issued = [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        n = len(self.calls) + 1
        self.calls.append(req)
        if n in self.fail:
            return httpx.Response(self.fail[n], json={"errors": "x"}, headers=self._hdr())
        if req.url.path.endswith("/connect/oauth2/token"):
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"{TOKEN}-{self.tokens_issued}", "expires_in": self.token_ttl})
        if req.headers.get("authorization", "") != f"Bearer {TOKEN}-{self.tokens_issued}":
            return httpx.Response(401, json={"errors": "bad token"}, headers=self._hdr())
        self.remaining -= 1
        loc = req.url.params["filter.locationId"]
        asked = req.url.params["filter.productId"].split(",")
        data = [self.catalog[loc][u] for u in asked if self.catalog.get(loc, {}).get(u)]
        return httpx.Response(200, json={"data": data}, headers=self._hdr())

    def _hdr(self):
        return {"x-ratelimit-limit-day": str(self.limit), "x-ratelimit-remaining-day": str(self.remaining)}

    @property
    def product_calls(self):
        return [c for c in self.calls if c.url.path.endswith("/products")]


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self): return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_client(api, clock=None, min_gap=1.0):
    clock = clock or Clock()
    http = httpx.Client(transport=httpx.MockTransport(api))
    return k.KrogerClient(http, "cid", SECRET, clock=clock.now, sleep=clock.sleep, min_gap=min_gap), clock


def upcs(n):
    return [f"{i:013d}" for i in range(1, n + 1)]


def catalog_for(locs, n, **price_by_loc):
    return {loc: {u: product(upc=u, regular=1.0 + i / 100, pid=f"id{u}") for i, u in enumerate(upcs(n))} for loc in locs}


def basket_of(n):
    return [k.BasketItem(k.normalize_gtin(u)) for u in upcs(n)]


def panel_of(*locs):
    return [k.PanelStore(l, f"metro-{l}", "KROGER", "n", None) for l in locs]


# ---- the client's stop rules -------------------------------------------------------------------

def test_token_is_fetched_once_and_reused_then_refreshed_before_expiry():
    api = FakeApi(catalog_for(["A"], 1))
    c, clock = make_client(api, min_gap=0)
    c.products("A", upcs(1))
    c.products("A", upcs(1))
    assert api.tokens_issued == 1
    clock.t += 1679                                # token lives 1800 s; refresh happens 120 s early => at 1680 s
    c.products("A", upcs(1))
    assert api.tokens_issued == 1
    clock.t += 2
    c.products("A", upcs(1))
    assert api.tokens_issued == 2


def test_token_request_uses_basic_auth_and_the_scope_the_spike_verified_and_never_leaks_the_secret():
    api = FakeApi(catalog_for(["A"], 1))
    c, _ = make_client(api)
    c.products("A", upcs(1))
    tok = api.calls[0]
    assert tok.headers["authorization"].startswith("Basic ") and b"product.compact" in tok.content
    assert SECRET not in str(api.product_calls[0].url) and SECRET not in api.product_calls[0].headers["authorization"]


def test_requests_are_at_least_min_gap_apart():
    api = FakeApi(catalog_for(["A"], 1))
    c, clock = make_client(api, min_gap=1.0)
    for _ in range(3):
        c.products("A", upcs(1))
    assert clock.sleeps and all(s >= 0.99 for s in clock.sleeps) and c.requests == 4   # token + 3


def test_rate_limit_headers_are_read_and_the_50_percent_reserve_is_enforced():
    api = FakeApi(catalog_for(["A"], 1), limit=100, remaining=52)
    c, _ = make_client(api)
    assert not c.over_cap()
    c.products("A", upcs(1))                       # remaining -> 51
    assert (c.limit_day, c.remaining_day) == (100, 51) and not c.over_cap()
    c.products("A", upcs(1))                       # remaining -> 50 == 50% of 100
    assert c.over_cap()


@pytest.mark.parametrize("status,expected", [(429, "rate_limited"), (403, "blocked"), (500, "error"), (503, "error")])
def test_refusals_stop_the_run_and_are_never_retried(status, expected):
    api = FakeApi(catalog_for(["A"], 1), fail={2: status})
    c, _ = make_client(api)
    with pytest.raises(k.Stop) as e:
        c.products("A", upcs(1))
    assert e.value.status == expected and len(api.calls) == 2 and c.http_errors == 1


def test_one_expired_token_is_refreshed_once():
    api = FakeApi(catalog_for(["A"], 1))
    c, _ = make_client(api)
    c.products("A", upcs(1))
    api.tokens_issued += 1                         # the server now rejects the token we cached
    calls_before = len(api.calls)
    assert len(c.products("A", upcs(1))) == 1      # 401 -> new token -> the same request succeeds
    assert api.tokens_issued == 3                  # first token, the simulated expiry, one real refresh
    assert [r.url.path.rsplit("/", 1)[-1] for r in api.calls[calls_before:]] == ["products", "token", "products"]


def test_a_second_401_stops_instead_of_looping():
    class Always401:
        def __init__(self): self.n = 0
        def __call__(self, req):
            self.n += 1
            if req.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
            return httpx.Response(401, json={})
    h = Always401()
    clock = Clock()
    c = k.KrogerClient(httpx.Client(transport=httpx.MockTransport(h)), "i", SECRET, clock=clock.now, sleep=clock.sleep)
    with pytest.raises(k.Stop) as e:
        c.products("A", upcs(1))
    assert e.value.status == "blocked" and h.n == 4        # token, 401, token, 401: then it stops


def test_token_endpoint_refusal_stops():
    api = FakeApi({}, fail={1: 401})
    c, _ = make_client(api)
    with pytest.raises(k.Stop) as e:
        c.products("A", upcs(1))
    assert e.value.status == "blocked" and len(api.calls) == 1


# ---- whole runs ---------------------------------------------------------------------------------

def do_run(api, basket, panel, repo=None, **kw):
    client, _ = make_client(api)
    repo = repo or k.MemoryRepo()
    return k.run(basket, panel, client, repo, now=lambda: T0, **kw), repo, api


def test_run_batches_the_basket_per_store_and_writes_each_product_once():
    api = FakeApi(catalog_for(["A", "B"], 10))
    res, repo, _ = do_run(api, basket_of(10), panel_of("A", "B"))
    # 10 products = 2 batches (8 + 2); 2 stores => 4 product calls, plus one token request
    assert len(api.product_calls) == 4 and res["requests"] == 5 and res["status"] == "ok"
    assert res["rows_written"] == 20 and len(repo.rows) == 20 and res["priced"] == 20
    sizes = sorted(len(c.url.params["filter.productId"].split(",")) for c in api.product_calls)
    assert sizes == [2, 2, 8, 8]
    assert all(len(u) == 13 for c in api.product_calls for u in c.url.params["filter.productId"].split(","))
    assert repo.runs[0]["status"] == "ok" and repo.runs[0]["rows"] == 20


def test_second_run_writes_nothing_when_nothing_changed():
    cat = catalog_for(["A"], 3)
    first = do_run(FakeApi(cat), basket_of(3), panel_of("A"))
    previous = {("A", o.gtin): k.Previous(o.observed_at - timedelta(days=1), o.status, o.price_cents, o.promo_cents,
                                         o.size_value, o.size_unit) for o in first[1].rows}
    res, repo, _ = do_run(FakeApi(cat), basket_of(3), panel_of("A"), repo=k.MemoryRepo(previous=previous))
    assert res["rows_written"] == 0 and res["unchanged"] == 3 and repo.rows == []


def test_only_the_changed_price_is_written():
    cat = catalog_for(["A"], 3)
    first = do_run(FakeApi(cat), basket_of(3), panel_of("A"))
    previous = {("A", o.gtin): k.Previous(o.observed_at - timedelta(days=1), o.status, o.price_cents, o.promo_cents,
                                         o.size_value, o.size_unit) for o in first[1].rows}
    cat["A"][upcs(3)[1]] = product(upc=upcs(3)[1], regular=9.99, pid="x")
    res, repo, _ = do_run(FakeApi(cat), basket_of(3), panel_of("A"), repo=k.MemoryRepo(previous=previous))
    assert [o.price_cents for o in repo.rows] == [999] and res["unchanged"] == 2


def test_a_product_the_api_does_not_return_produces_no_row_and_is_not_a_drop():
    cat = catalog_for(["A"], 3)
    cat["A"][upcs(3)[2]] = None
    res, repo, _ = do_run(FakeApi(cat), basket_of(3), panel_of("A"))
    assert res["returned"] == 2 and len(repo.rows) == 2


def test_unpriced_products_are_recorded_as_unpriced():
    cat = catalog_for(["A"], 2)
    cat["A"][upcs(2)[0]] = product(upc=upcs(2)[0], regular=None)
    res, repo, _ = do_run(FakeApi(cat), basket_of(2), panel_of("A"))
    assert (res["priced"], res["unpriced"]) == (1, 1)
    assert sorted(o.status for o in repo.rows) == ["priced", "unpriced"]


def test_products_we_did_not_ask_for_and_unusable_rows_are_counted_and_ignored():
    cat = catalog_for(["A"], 2)
    cat["A"][upcs(2)[0]] = product(upc="9999999999999")        # a different UPC than requested
    cat["A"][upcs(2)[1]] = product(upc=upcs(2)[1], regular=-1)  # unusable price
    res, repo, _ = do_run(FakeApi(cat), basket_of(2), panel_of("A"))
    assert res["bad_rows"] == 2 and repo.rows == [] and res["status"] == "ok"


def test_kill_switch_means_no_requests_at_all():
    api = FakeApi(catalog_for(["A"], 3))
    res, repo, _ = do_run(api, basket_of(3), panel_of("A"), repo=k.MemoryRepo(enabled=False))
    assert res["status"] == "disabled" and api.calls == [] and repo.runs == []


def test_empty_basket_or_panel_is_an_error_without_requests():
    api = FakeApi({})
    assert do_run(api, [], panel_of("A"))[0]["status"] == "error"
    assert do_run(api, basket_of(1), [])[0]["status"] == "error" and api.calls == []


def test_max_requests_stops_the_run_and_every_store_gets_its_first_batch():
    api = FakeApi(catalog_for(["A", "B", "C"], 24))               # 3 batches x 3 stores = 9 product calls
    res, repo, _ = do_run(api, basket_of(24), panel_of("A", "B", "C"), max_requests=4)   # token + 3 product calls
    assert res["status"] == "partial" and "max-requests" in res["note"]
    assert sorted(c.url.params["filter.locationId"] for c in api.product_calls) == ["A", "B", "C"]
    assert repo.runs[0]["status"] == "partial"


def test_quota_reserve_stops_the_run():
    api = FakeApi(catalog_for(["A"], 24), limit=100, remaining=52)
    res, _, _ = do_run(api, basket_of(24), panel_of("A"))
    assert res["status"] == "partial" and "reserve" in res["note"] and len(api.product_calls) == 2


def test_a_429_mid_run_ends_it_immediately_and_is_recorded():
    api = FakeApi(catalog_for(["A", "B"], 16), fail={4: 429})
    res, repo, _ = do_run(api, basket_of(16), panel_of("A", "B"))
    assert res["status"] == "rate_limited" and len(api.calls) == 4 and res["http_errors"] == 1
    assert repo.runs[0]["status"] == "rate_limited" and k.exit_code(res) == k.EXIT_STOPPED


def test_an_unexpected_exception_is_recorded_as_an_error_not_ok():
    class Exploding(k.MemoryRepo):
        def write(self, *a, **kw):
            raise RuntimeError("disk full")
    api = FakeApi(catalog_for(["A"], 1))
    with pytest.raises(RuntimeError):
        do_run(api, basket_of(1), panel_of("A"), repo=(r := Exploding()))
    assert r.runs[0]["status"] == "error"


def test_exit_codes():
    assert k.exit_code({"status": "ok"}) == 0 and k.exit_code({"status": "disabled"}) == 0
    assert k.exit_code({"status": "partial"}) == 1
    assert k.exit_code({"status": "blocked"}) == 3 and k.exit_code({"status": "rate_limited"}) == 3


# ---- command line --------------------------------------------------------------------------------

def test_cli_refuses_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    monkeypatch.delenv("KROGER_CLIENT_SECRET", raising=False)
    assert k.main(["run", "--dry-run"]) == k.EXIT_CONFIG


def test_cli_needs_a_database_url_unless_dry_run(monkeypatch):
    monkeypatch.setenv("KROGER_CLIENT_ID", "i")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert k.main(["run"]) == k.EXIT_CONFIG


def test_cli_bad_config_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("KROGER_CLIENT_ID", "i")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", SECRET)
    bad = tmp_path / "b.yaml"
    bad.write_text("products:\n  - {gtin: nope}\n")
    assert k.main(["run", "--dry-run", "--basket", str(bad)]) == k.EXIT_CONFIG


def test_dry_run_prints_only_counts_and_never_the_secret_or_token(monkeypatch, capsys, tmp_path):
    api = FakeApi(catalog_for(["62000115"], 2))
    real = httpx.Client
    monkeypatch.setattr(k.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(api), headers=kw.get("headers")))
    monkeypatch.setattr(k.time, "sleep", lambda s: None)
    monkeypatch.setenv("KROGER_CLIENT_ID", "client-id-xyz")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", SECRET)
    basket, panel = tmp_path / "b.yaml", tmp_path / "p.yaml"
    basket.write_text(yaml.safe_dump({"products": [{"gtin": u} for u in upcs(2)]}))
    panel.write_text(yaml.safe_dump({"stores": [{"metro": "denver", "location_id": "62000115", "banner": "KS"}]}))
    code = k.main(["run", "--dry-run", "--basket", str(basket), "--panel", str(panel)])
    out = capsys.readouterr()
    assert code == 0 and json.loads(out.out)["dry_run"] is True
    for text in (out.out, out.err):
        assert SECRET not in text and TOKEN not in text and "client-id-xyz" not in text
    assert api.product_calls[0].headers["user-agent"] == k.UA

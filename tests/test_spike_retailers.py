"""The stop-on-challenge logic is the safety rule of the retailer collectors, so it is tested.

All HTML here is synthetic; no retailer content is stored in the repo.
"""
import importlib.util
import json
import pathlib

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "spike_retailers", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "spike_retailers.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)


def resp(status=200, text=""):
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://example.test/p/1"))


def next_data(obj):
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(obj)}</script></html>'


@pytest.mark.parametrize("status", [401, 403, 412, 429, 503])
def test_block_statuses_stop(status):
    verdict, _ = sr.classify(resp(status, "anything"))
    assert verdict == "blocked"


def test_unexpected_non_200_stops():
    assert sr.classify(resp(302, ""))[0] == "blocked"


def test_200_challenge_page_without_product_data_stops():
    body = "<html><title>Robot or human?</title><div id='px-captcha'></div></html>"
    verdict, reason = sr.classify(resp(200, body))
    assert verdict == "challenge" and "challenge" in reason


def test_200_with_product_data_is_ok_even_if_it_mentions_captcha_script():
    body = next_data({"props": {}}) + "<script>/* captcha lib */</script>"
    assert sr.classify(resp(200, body))[0] == "ok"


def test_plain_200_without_markers_is_ok_but_flagged_unrecognised():
    verdict, reason = sr.classify(resp(200, "<html>hello</html>"))
    assert verdict == "ok" and "no recognised product data" in reason


def test_price_found_for_the_pages_own_item_not_an_ad_slot():
    data = {"props": {"initialData": {
        "product": {"usItemId": "421705528", "name": "Eggs",
                    "priceInfo": {"currentPrice": {"price": 1.67}, "unitPrice": {"priceString": "7.0 c/oz"}}},
        "ad": {"products": [{"usItemId": "10449724", "priceInfo": {"currentPrice": {"price": 5.96}}}]}}}}
    out = sr.find_item_price(next_data(data), "421705528")
    assert out == {"price_found": True, "price": 1.67, "unit_price": "7.0 c/oz", "name": "Eggs"}


def test_price_not_found_when_only_other_items_have_prices():
    data = {"ad": {"products": [{"usItemId": "10449724", "priceInfo": {"currentPrice": {"price": 5.96}}}]}}
    assert sr.find_item_price(next_data(data), "421705528")["price_found"] is False


def test_price_not_found_without_next_data_or_with_bad_json():
    assert sr.find_item_price("<html></html>", "1")["price_found"] is False
    bad = '<script id="__NEXT_DATA__">{not json</script>'
    assert sr.find_item_price(bad, "1")["price_found"] is False


@pytest.mark.parametrize("url,expected", [
    ("https://www.walmart.com/ip/Great-Value-Eggs/421705528", "421705528"),
    ("https://www.target.com/p/some-slug/-/A-14713534", "14713534"),
    ("https://www.target.com/p/some-slug/-/A-14713534?x=1", "14713534"),
    ("https://www.walmart.com/search?q=eggs", ""),
])
def test_item_id_from_url(url, expected):
    assert sr.item_id_from_url(url) == expected


# ---- run(): the orchestration that actually enforces "polite" ---------------------------------

HOST = "https://shop.example.test"
URLS = [f"{HOST}/p/a/-/A-1000001", f"{HOST}/p/b/-/A-1000002", f"{HOST}/p/c/-/A-1000003"]
ALLOW_ALL = "User-agent: *\nDisallow: /search\n"


@pytest.fixture(autouse=True)
def _no_sleep_no_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    monkeypatch.setattr(sr, "OUT", tmp_path / "raw")


def server(robots_status=200, robots=ALLOW_ALL, page_status=200, page_body=None):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots)
        body = page_body if page_body is not None else next_data({"props": {}})
        return httpx.Response(page_status, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": sr.UA},
                          follow_redirects=True)
    return client, seen


def test_run_fetches_each_page_when_everything_is_ok():
    client, seen = server()
    result = sr.run("shop", URLS[:2], client)
    assert seen == ["/robots.txt", "/p/a/-/A-1000001", "/p/b/-/A-1000002"]
    assert result["stopped"] is None


def test_run_stops_after_the_first_block_and_never_retries():
    client, seen = server(page_status=403)
    result = sr.run("shop", URLS[:2], client)
    assert seen == ["/robots.txt", "/p/a/-/A-1000001"]       # exactly one product request
    assert "blocked" in result["stopped"] and "no retry" in result["stopped"].lower()


def test_run_stops_on_a_200_challenge_page():
    client, seen = server(page_body="<html>Robot or human?<div id='px-captcha'></div></html>")
    result = sr.run("shop", URLS[:2], client)
    assert len(seen) == 2                                     # robots + the one page
    assert "challenge" in result["stopped"]


def test_run_stops_on_429():
    client, seen = server(page_status=429)
    result = sr.run("shop", URLS[:2], client)
    assert len(seen) == 2 and "blocked" in result["stopped"]


def test_run_never_requests_a_robots_disallowed_url():
    client, seen = server(robots="User-agent: *\nDisallow: /p/\n")
    result = sr.run("shop", URLS[:2], client)
    assert seen == ["/robots.txt"]
    assert result["pages"][0]["robots"] == "DISALLOWED" and result["pages"][0]["fetched"] is False
    assert "robots.txt disallows" in result["stopped"]


def test_run_fails_closed_when_robots_is_unavailable():
    client, seen = server(robots_status=500)
    result = sr.run("shop", URLS[:2], client)
    assert seen == ["/robots.txt"] and result["stopped"]


def test_run_never_exceeds_max_pages():
    client, seen = server()
    sr.run("shop", URLS, client)                              # 3 URLs offered
    assert len([p for p in seen if p != "/robots.txt"]) == sr.MAX_PAGES == 2


def test_run_sends_only_the_identifying_user_agent_and_accept_headers_we_set():
    captured = []

    def handler(req):
        captured.append(req.headers)
        return httpx.Response(200, text=ALLOW_ALL if req.url.path == "/robots.txt" else next_data({}))

    with httpx.Client(transport=httpx.MockTransport(handler),
                      headers={"User-Agent": sr.UA, "Accept": "text/html"}) as client:
        sr.run("shop", URLS[:1], client)
    for h in captured:
        assert h["user-agent"] == sr.UA and "cost-bot-ai/cost-bot" in h["user-agent"]
        assert "cookie" not in h and "authorization" not in h and "referer" not in h

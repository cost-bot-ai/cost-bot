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

"""First-party vs marketplace sampling: the stop rules and the 'never guess' classification are tested.

All HTML is synthetic; no Walmart content is stored.
"""
import gzip
import importlib.util
import json
import pathlib

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "spike_walmart_sellers", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "spike_walmart_sellers.py")
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def page(product):
    data = {"props": {"pageProps": {"initialData": {"data": {"product": product}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


FIRST = {"sellerType": "INTERNAL", "sellerDisplayName": "Walmart.com", "offerType": "ONLINE_AND_STORE",
         "name": "Great Value Eggs", "priceInfo": {"currentPrice": {"price": 1.67}}}
MARKET = {"sellerType": "EXTERNAL", "sellerDisplayName": "Some Seller LLC", "offerType": "ONLINE_ONLY",
          "name": "Wall Poster", "priceInfo": {"currentPrice": {"price": 19.99}}}


# ---- seller_info / classification: never guess ------------------------------------------------

def test_first_party_page():
    info = ws.seller_info(page(FIRST))
    assert info["sellerType"] == "INTERNAL" and info["price"] == 1.67 and ws.classify_seller(info) == "first_party"


def test_marketplace_page():
    info = ws.seller_info(page(MARKET))
    assert info["sellerDisplayName"] == "Some Seller LLC" and ws.classify_seller(info) == "marketplace"


@pytest.mark.parametrize("html", ["<html></html>", '<script id="__NEXT_DATA__">{bad json</script>',
                                  page(None), page("not a dict"), page({"name": "no seller fields"})])
def test_missing_or_odd_data_is_unknown_not_a_guess(html):
    assert ws.classify_seller(ws.seller_info(html)) == "unknown"


def test_unrecognised_seller_type_is_unknown():
    assert ws.classify_seller({"sellerType": "SOMETHING_NEW"}) == "unknown"


# ---- URL handling ----------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.walmart.com/ip/Great-Value-Whole-Milk/10450114", True),
    ("https://www.walmart.com/ip/Equate-Ibuprofen-200-mg/1234567", True),
    ("https://www.walmart.com/ip/Parent-s-Choice-Diapers/555555", True),
    ("https://www.walmart.com/ip/Greater-Good-Poster/777777", False),     # prefix must end at a word boundary
    ("https://www.walmart.com/ip/Onnit-Protein/888888", False),
    ("https://www.walmart.com/ip/Netflix-Poster/999999", False),
    ("https://www.walmart.com/browse/food", False),
])
def test_private_label_slug_detection(url, expected):
    assert ws.is_private_label(url) is expected


def test_slug_extraction():
    assert ws.slug("https://www.walmart.com/ip/Great-Value-Milk/123") == "great-value-milk"
    assert ws.slug("https://www.walmart.com/cp/food") == ""


def test_sample_is_seeded_and_bounded():
    urls = [f"https://www.walmart.com/ip/item-{i}/{i}" for i in range(1000)]
    a, b = ws.draw_sample(urls, 20, seed=7), ws.draw_sample(urls, 20, seed=7)
    assert a == b and len(set(a)) == 20 and ws.draw_sample(urls, 20, seed=8) != a
    assert len(ws.draw_sample(urls[:5], 20, seed=7)) == 5


def test_urls_from_sitemaps_reads_gz_and_plain_and_keeps_only_ip_urls(tmp_path):
    xml = ("<urlset><url><loc>https://www.walmart.com/ip/a/1</loc></url>"
           "<url><loc>https://www.walmart.com/browse/x</loc></url></urlset>")
    (tmp_path / "a.xml.gz").write_bytes(gzip.compress(xml.encode()))
    (tmp_path / "b.xml").write_text(xml.replace("/ip/a/1", "/ip/b/2"))
    got = ws.urls_from_sitemaps([str(tmp_path / "a.xml.gz"), str(tmp_path / "b.xml")])
    assert got == ["https://www.walmart.com/ip/a/1", "https://www.walmart.com/ip/b/2"]


# ---- run(): polite behaviour ---------------------------------------------------------------

URLS = [f"https://www.walmart.com/ip/item-{i}/{1000000 + i}" for i in range(30)]


def server(robots="User-agent: *\nDisallow: /search\n", status=200, body=None):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(status, text=body if body is not None else page(FIRST))

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": ws.UA}), seen


def test_run_classifies_each_page_and_summarizes():
    client, seen = server()
    res = ws.run(URLS[:3], client)
    s = ws.summarize(res)
    assert len(seen) == 4 and s["fetched"] == 3 and s["counts"]["first_party"] == 3 and res["stopped"] is None


@pytest.mark.parametrize("status", [403, 429, 503])
def test_run_stops_after_first_block_without_retry(status):
    client, seen = server(status=status)
    res = ws.run(URLS[:5], client)
    assert seen == ["/robots.txt", urlparse_path(URLS[0])] and "no retry" in res["stopped"].lower()


def test_run_stops_on_a_200_challenge_page():
    client, seen = server(body="<html>Robot or human? px-captcha</html>")
    res = ws.run(URLS[:5], client)
    assert len(seen) == 2 and "challenge" in res["stopped"]


def test_run_never_fetches_a_robots_disallowed_url():
    client, seen = server(robots="User-agent: *\nDisallow: /ip/\n")
    res = ws.run(URLS[:5], client)
    assert seen == ["/robots.txt"] and "robots.txt disallows" in res["stopped"]


def test_run_has_a_hard_page_ceiling_even_if_asked_for_more():
    client, seen = server()
    ws.run(URLS, client, budget=1000)
    assert len(seen) - 1 == ws.MAX_PAGES == 25


def urlparse_path(u):
    return ws.urlparse(u).path

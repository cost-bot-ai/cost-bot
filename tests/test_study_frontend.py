"""The front-end study reads other sites' public JavaScript, so what it may fetch and when it must stop are tested.

All JavaScript and HTML here is synthetic; no site code is stored in the repo.
"""
import importlib.util
import pathlib
import urllib.robotparser

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "study_frontend", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "study_frontend.py")
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

BASE = "https://www.shop.test/"
CDN = "https://cdn.shop.test"


@pytest.fixture(autouse=True)
def _record_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    return slept


# ---- which files may be fetched -------------------------------------------------------------

def page(*srcs):
    return "<html>" + "".join(f'<script src="{s}"></script>' for s in srcs) + "</html>"


def test_only_nextjs_static_chunks_are_fetchable():
    html = page(f"{CDN}/_next/static/chunks/app-1.js", "/_next/static/chunks/main-2.js",
                "/u-Odd-Looking-Bot-Management-Path",                  # injected anti-bot script: never fetched
                "https://tracker.example/analytics.js",                # third party
                "http://www.shop.test/_next/static/chunks/insecure.js",  # not https
                f"{CDN}/_next/static/css/styles.css",                  # not JavaScript
                f"{CDN}/_next/static/chunks/app-1.js")                 # duplicate
    ok, excluded = sf.script_urls(html, BASE)
    assert ok == [f"{CDN}/_next/static/chunks/app-1.js", "https://www.shop.test/_next/static/chunks/main-2.js"]
    assert any("Bot-Management" in u for u in excluded) and len(excluded) == 4


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": sf.UA})


URLS = [f"{CDN}/_next/static/chunks/c{i}.js" for i in range(40)]


def server(robots="User-agent: *\nDisallow: /search\n", status=200, ctype="application/javascript", body=b"var a=1;"):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    return client(handler), seen


def test_fetch_writes_files_and_paces_requests(tmp_path, _record_sleep):
    c, seen = server()
    res = sf.fetch_chunks(c, URLS[:3], tmp_path)
    assert res["stopped"] is None and [f["file"] for f in res["fetched"]] == ["c0.js", "c1.js", "c2.js"]
    assert seen[0] == "/robots.txt" and len(seen) == 4
    assert len(_record_sleep) >= 3 and all(s >= 5.0 for s in _record_sleep)       # >= 5 s between requests
    assert (tmp_path / "c0.js").read_bytes() == b"var a=1;"


def test_hard_ceiling_of_30_files_even_if_asked_for_more(tmp_path):
    c, seen = server()
    sf.fetch_chunks(c, URLS, tmp_path, budget=1000)
    assert len([p for p in seen if p != "/robots.txt"]) == sf.MAX_PER_SITE == 30


@pytest.mark.parametrize("status", [403, 429, 503, 404])
def test_stops_on_first_non_200_without_retry(tmp_path, status):
    c, seen = server(status=status)
    res = sf.fetch_chunks(c, URLS[:5], tmp_path)
    assert len(seen) == 2 and f"HTTP {status}" in res["stopped"] and "no retry" in res["stopped"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("ctype,body", [("text/html", b"<html>Robot or human?</html>"),
                                        ("application/javascript", b"<html>challenge</html>")])
def test_html_where_javascript_was_expected_is_treated_as_a_challenge(tmp_path, ctype, body):
    c, seen = server(ctype=ctype, body=body)
    res = sf.fetch_chunks(c, URLS[:5], tmp_path)
    assert "possible challenge" in res["stopped"] and len(seen) == 2 and list(tmp_path.iterdir()) == []


def test_robots_disallow_stops_before_any_chunk_is_requested(tmp_path):
    c, seen = server(robots="User-agent: *\nDisallow: /_next/\n")
    res = sf.fetch_chunks(c, URLS[:5], tmp_path)
    assert seen == ["/robots.txt"] and "disallows" in res["stopped"]


def test_robots_unavailable_fails_closed(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(500, text="oops")

    res = sf.fetch_chunks(client(handler), URLS[:3], tmp_path)
    assert seen == ["/robots.txt"] and res["stopped"]


@pytest.mark.parametrize("bad", [f"{CDN}/u-Odd-Path", f"{CDN}/_next/static/css/a.css", f"{CDN}/api/graphql",
                                 f"{CDN}/_next/data/x.json"])
def test_refuses_any_url_that_is_not_a_static_chunk(tmp_path, bad):
    c, seen = server()
    res = sf.fetch_chunks(c, [bad] + URLS[:2], tmp_path)
    assert seen == [] and "refused to fetch" in res["stopped"]


def test_existing_files_are_not_fetched_again(tmp_path):
    (tmp_path / "c0.js").write_bytes(b"cached")
    c, seen = server()
    res = sf.fetch_chunks(c, URLS[:2], tmp_path)
    assert res["skipped_existing"] == 1 and [f["file"] for f in res["fetched"]] == ["c1.js"]
    assert (tmp_path / "c0.js").read_bytes() == b"cached"


# ---- what the analyzer finds ----------------------------------------------------------------

def test_finds_cookie_names_from_the_common_patterns():
    js = ('document.cookie="storePref="+id+"; path=/";'
          'Cookies.set("preferred_store",id);cookies.get("my_store");setCookie("loc_nid",1);removeCookie("old");')
    found = sf.analyze_js(js)["cookies"]
    assert {"storePref", "preferred_store", "my_store", "loc_nid", "old"} <= set(found)


def test_finds_storage_keys_url_params_endpoints_and_store_functions():
    js = ('localStorage.setItem("selectedStore",x);sessionStorage.getItem("zip");'
          'u.searchParams.set("storeId",5);fetch("/api/stores/select",{});fetch("/graphql");'
          'fetch("https://api.shop.test/v2/locations");go("/p?store_nid=7");setPreferredStore(1);selectStore(2);')
    a = sf.analyze_js(js)
    assert {"selectedStore", "zip"} == set(a["storage_keys"])
    assert {"storeId", "store_nid"} <= set(a["url_params"])
    assert {"/api/stores/select", "/graphql", "https://api.shop.test/v2/locations"} <= set(a["endpoints"])
    assert {"setPreferredStore", "selectStore"} <= set(a["store_functions"])


def test_absolute_urls_count_only_when_they_look_like_endpoints():
    js = ('a("https://api.shop.test/v2/anything");b("https://www.shop.test/stores/lookup");'
          'c("https://fonts.googleapis.com/css");d("https://www.shop.test/about");e("https://cdn.shop.test/logo.png")')
    eps = set(sf.analyze_js(js)["endpoints"])
    assert eps == {"https://api.shop.test/v2/anything", "https://www.shop.test/stores/lookup"}


def test_unrelated_javascript_yields_nothing():
    a = sf.analyze_js("function add(a,b){return a+b} const x = 'hello'; console.log(x)")
    assert all(v == [] for v in a.values())


def test_store_relevance_filter():
    assert sf.store_relevant("preferred_store") and sf.store_relevant("zipCode") and sf.store_relevant("PICKUP")
    assert not sf.store_relevant("theme") and not sf.store_relevant("analytics_id")


def test_merge_combines_and_dedupes():
    m = sf.merge_findings([{"cookies": ["a", "b"]}, {"cookies": ["b", "c"], "endpoints": ["/api/x"]}])
    assert m["cookies"] == ["a", "b", "c"] and m["endpoints"] == ["/api/x"] and m["url_params"] == []


def test_contexts_returns_a_few_short_snippets():
    js = "x" * 300 + "STORE" + "y" * 300 + " STORE " + "z" * 50 + "STORE" * 1
    out = sf.contexts(js, "STORE", width=20, limit=2)
    assert len(out) == 2 and all(len(s) <= 45 for s in out)


# ---- endpoints vs robots.txt: finding an endpoint is not permission to call it ---------------

def parser(text):
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp


def test_endpoint_verdicts_mark_disallowed_allowed_and_unknown():
    robots = {"www.shop.test": parser("User-agent: *\nDisallow: /graphql\nDisallow: */ajax/*\n")}
    v = {e["endpoint"]: e["robots"] for e in sf.endpoint_verdicts(
        ["/graphql", "/api/stores", "/x/ajax/y", "https://other.test/api/z"], "www.shop.test", robots)}
    assert v["/graphql"].startswith("DISALLOWED")
    assert v["/api/stores"] == "allowed"
    assert v["https://other.test/api/z"].startswith("unknown") and "off-limits" in v["https://other.test/api/z"]

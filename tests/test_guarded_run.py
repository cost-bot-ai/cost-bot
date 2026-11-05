"""The guarded runner turns the runbook's rules into code, so each rule is tested, including that NO retailer
request is made when a guard refuses. All traffic is a mock; no real network, no real IPs.
"""
import datetime as dt
import gzip
import importlib.util
import json
import pathlib
import types

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "guarded_run", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "guarded_run.py")
gr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gr)

HOME = "81.2.69.142"        # stands in for the user's real public IP
VPN = "93.184.216.34"       # stands in for the VPN exit address
NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


# ---- pure rules ------------------------------------------------------------------------------

@pytest.mark.parametrize("egress,home,ok", [
    (VPN, HOME, True),
    (HOME, HOME, False),                      # leaking through the home connection
    (VPN, "", False), (VPN, None, False), (VPN, "not-an-ip", False),
    (VPN, "192.168.1.10", False),             # a LAN address is not "your public IP"
    ("10.0.0.2", HOME, False),                # a private egress address is not a VPN exit
    ("garbage", HOME, False),
])
def test_check_egress(egress, home, ok):
    assert gr.check_egress(egress, home)[0] is ok


def test_ipv6_is_handled():
    assert gr.check_egress("2606:4700:4700::1111", HOME)[0] is True
    assert gr.check_egress("2606:4700:4700::1111", "2606:4700:4700::1111")[0] is False


def hours_ago(h):
    return (NOW - dt.timedelta(hours=h)).isoformat()


@pytest.mark.parametrize("state,allowed", [
    (None, True), ({}, True),
    ({"last_run_at": hours_ago(2), "blocked": True}, False),       # blocked 2 h ago: wait 24 h
    ({"last_run_at": hours_ago(23.9), "blocked": True}, False),
    ({"last_run_at": hours_ago(24.1), "blocked": True}, True),
    ({"last_run_at": hours_ago(0.2), "blocked": False}, False),    # any run: 1 h minimum gap
    ({"last_run_at": hours_ago(1.1), "blocked": False}, True),
    ({"last_run_at": (NOW + dt.timedelta(hours=3)).isoformat(), "blocked": False}, False),  # clock skew
])
def test_cooldown_decision(state, allowed):
    assert gr.cooldown_decision(state, NOW)[0] is allowed


@pytest.mark.parametrize("state", [{"last_run_at": "yesterday"}, {"last_run_at": "2026-11-05T12:00:00"},
                                   {"last_run_at": None}, {"blocked": True, "last_run_at": 5}])
def test_unreadable_timestamp_fails_closed(state):
    with pytest.raises(gr.GuardError):
        gr.cooldown_decision(state, NOW)


def test_corrupt_state_file_fails_closed(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    with pytest.raises(gr.GuardError):
        gr.load_state(p)
    p.write_text("[1,2]")
    with pytest.raises(gr.GuardError):
        gr.load_state(p)


def test_save_state_round_trips_and_leaves_no_temp_file(tmp_path):
    p = tmp_path / "sub" / "state.json"
    gr.save_state(p, {"last_run_at": NOW.isoformat(), "blocked": False})
    assert gr.load_state(p)["blocked"] is False and not list(p.parent.glob("*.tmp"))


# ---- the guarded flow against a mock network --------------------------------------------------

def sitemap_gz(n=30):
    xml = "<urlset>" + "".join(f"<url><loc>https://www.walmart.com/ip/item-{i}/{1000000 + i}</loc></url>"
                               for i in range(n)) + "</urlset>"
    return gzip.compress(xml.encode())


def product_page(seller="INTERNAL"):
    prod = {"sellerType": seller, "sellerDisplayName": "Walmart.com" if seller == "INTERNAL" else "Some Seller",
            "offerType": "ONLINE_ONLY", "name": "x", "priceInfo": {"currentPrice": {"price": 1.5}}}
    data = {"props": {"pageProps": {"initialData": {"data": {"product": prod}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


class Net:
    """Mock internet. `seen` records every request as 'host/path'."""

    def __init__(self, egress=VPN, echo_fails=False, page_status=200, page_body=None, robots_ok=True,
                 sitemap_status=200):
        self.seen = []
        self.egress, self.echo_fails, self.page_status = egress, echo_fails, page_status
        self.page_body, self.robots_ok, self.sitemap_status = page_body, robots_ok, sitemap_status

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(f"{req.url.host}{req.url.path}")
        if req.url.host == "ip.example.test":
            if self.echo_fails:
                raise httpx.ConnectError("tunnel down", request=req)
            return httpx.Response(200, text=self.egress)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search\n" if self.robots_ok
                                  else "User-agent: *\nDisallow: /\n")
        if req.url.path.endswith(".xml.gz"):
            return httpx.Response(self.sitemap_status, content=sitemap_gz() if self.sitemap_status == 200 else b"")
        body = self.page_body if self.page_body is not None else product_page()
        return httpx.Response(self.page_status, text=body)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler), headers={"User-Agent": gr.sr.UA})

    @property
    def walmart_requests(self):
        return [s for s in self.seen if s.startswith("www.walmart.com")]


def args_for(tmp_path, **over):
    base = dict(home_ip=HOME, ip_echo_url="https://ip.example.test/", state=tmp_path / "state.json",
                out_dir=tmp_path / "out", sitemap_dir=tmp_path / "sitemaps", sitemap_urls=gr.SITEMAP_URLS,
                n=5, seed=7, cooldown_hours=24.0)
    base.update(over)
    return types.SimpleNamespace(**base)


def outputs(tmp_path):
    return sorted((tmp_path / "out").glob("*.json"))


def test_clean_run_writes_output_and_state_and_no_ip_anywhere(tmp_path, capsys):
    net = Net()
    code = gr.run_check(args_for(tmp_path), net.client(), now=NOW)
    printed = capsys.readouterr().out
    assert code == gr.EXIT_OK
    files = outputs(tmp_path)
    assert len(files) == 1
    text = files[0].read_text() + (tmp_path / "state.json").read_text() + printed
    assert HOME not in text and VPN not in text                      # no IP in outputs, state or logs
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["blocked"] is False and state["stopped"] is None
    assert json.loads(files[0].read_text())["summary"]["counts"]["first_party"] == 5
    assert len([s for s in net.walmart_requests if "/ip/" in s]) == 5


def test_sitemaps_are_fetched_once_and_reused(tmp_path):
    net = Net()
    gr.run_check(args_for(tmp_path), net.client(), now=NOW)
    first = [s for s in net.seen if s.endswith(".xml.gz")]
    assert len(first) == 2
    net2 = Net()
    gr.run_check(args_for(tmp_path), net2.client(), now=NOW + dt.timedelta(hours=2))
    assert [s for s in net2.seen if s.endswith(".xml.gz")] == []


def test_traffic_from_the_home_ip_is_refused_before_any_retailer_request(tmp_path):
    net = Net(egress=HOME)
    assert gr.run_check(args_for(tmp_path), net.client(), now=NOW) == gr.EXIT_EGRESS
    assert net.walmart_requests == [] and outputs(tmp_path) == []


def test_tunnel_down_is_refused_before_any_retailer_request(tmp_path):
    net = Net(echo_fails=True)
    assert gr.run_check(args_for(tmp_path), net.client(), now=NOW) == gr.EXIT_EGRESS
    assert net.walmart_requests == []


@pytest.mark.parametrize("home,expected", [
    (None, gr.EXIT_CONFIG), ("", gr.EXIT_CONFIG), ("nope", gr.EXIT_CONFIG),   # not an IP at all: config error
    ("192.168.1.5", gr.EXIT_EGRESS),            # a LAN address passes the first check; the egress check rejects it
])
def test_missing_or_bad_home_ip_is_refused_with_no_retailer_requests(tmp_path, home, expected):
    net = Net()
    assert gr.run_check(args_for(tmp_path, home_ip=home), net.client(), now=NOW) == expected
    assert net.walmart_requests == [] and outputs(tmp_path) == []


def test_recent_block_means_no_requests_at_all_not_even_the_ip_check(tmp_path):
    gr.save_state(tmp_path / "state.json", {"last_run_at": hours_ago(2), "blocked": True, "stopped": "challenge"})
    net = Net()
    assert gr.run_check(args_for(tmp_path), net.client(), now=NOW) == gr.EXIT_COOLDOWN
    assert net.seen == []


def test_run_is_allowed_again_after_the_cooldown(tmp_path):
    gr.save_state(tmp_path / "state.json", {"last_run_at": hours_ago(25), "blocked": True, "stopped": "challenge"})
    assert gr.run_check(args_for(tmp_path), Net().client(), now=NOW) == gr.EXIT_OK


def test_corrupt_state_stops_the_run_with_no_requests(tmp_path):
    (tmp_path / "state.json").write_text("{broken")
    net = Net()
    assert gr.run_check(args_for(tmp_path), net.client(), now=NOW) == gr.EXIT_CONFIG
    assert net.seen == []


@pytest.mark.parametrize("kwargs", [
    dict(page_status=403), dict(page_status=429),
    dict(page_body="<html>Robot or human? px-captcha</html>"),
])
def test_a_block_during_the_run_stops_it_records_the_cooldown_and_never_retries(tmp_path, kwargs):
    net = Net(**kwargs)
    code = gr.run_check(args_for(tmp_path), net.client(), now=NOW)
    assert code == gr.EXIT_STOPPED
    assert len([s for s in net.walmart_requests if "/ip/" in s]) == 1            # one page, then stop
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["blocked"] is True
    assert gr.run_check(args_for(tmp_path), Net().client(),
                        now=NOW + dt.timedelta(hours=1)) == gr.EXIT_COOLDOWN   # and the next run is refused


def test_a_blocked_sitemap_fetch_also_starts_the_cooldown(tmp_path):
    net = Net(sitemap_status=403)
    assert gr.run_check(args_for(tmp_path), net.client(), now=NOW) == gr.EXIT_STOPPED
    assert json.loads((tmp_path / "state.json").read_text())["blocked"] is True
    assert [s for s in net.walmart_requests if "/ip/" in s] == []


def test_html_in_place_of_a_sitemap_is_treated_as_a_block(tmp_path):
    def handler(req):
        if req.url.host == "ip.example.test":
            return httpx.Response(200, text=VPN)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search\n")
        return httpx.Response(200, content=b"<html><body>Robot or human?</body></html>")
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": gr.sr.UA})
    assert gr.run_check(args_for(tmp_path), client, now=NOW) == gr.EXIT_STOPPED
    assert json.loads((tmp_path / "state.json").read_text())["blocked"] is True


def test_robots_disallow_stops_without_starting_the_block_cooldown(tmp_path):
    net = Net(robots_ok=False)
    assert gr.run_check(args_for(tmp_path), net.client(), now=NOW) == gr.EXIT_CONFIG
    assert [s for s in net.walmart_requests if "/ip/" in s] == [] and net.seen.count("www.walmart.com/robots.txt") >= 1


def test_the_page_ceiling_still_applies_through_the_runner(tmp_path):
    net = Net()
    gr.run_check(args_for(tmp_path, n=1000), net.client(), now=NOW)
    assert len([s for s in net.walmart_requests if "/ip/" in s]) <= gr.ws.MAX_PAGES


# ---- the egress-only command -------------------------------------------------------------------

def test_egress_command_reports_ok_fail_and_config(tmp_path, capsys):
    assert gr.egress_only(args_for(tmp_path), Net().client()) == gr.EXIT_OK
    assert gr.egress_only(args_for(tmp_path), Net(egress=HOME).client()) == gr.EXIT_EGRESS
    assert gr.egress_only(args_for(tmp_path), Net(echo_fails=True).client()) == gr.EXIT_EGRESS
    assert gr.egress_only(args_for(tmp_path, home_ip=None), Net().client()) == gr.EXIT_CONFIG
    printed = capsys.readouterr().out
    assert HOME not in printed and VPN not in printed


def test_egress_command_makes_no_retailer_requests(tmp_path):
    net = Net()
    gr.egress_only(args_for(tmp_path), net.client())
    assert net.walmart_requests == []


def test_parser_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("HOME_IP", HOME)
    monkeypatch.setenv("N_PAGES", "7")
    a = gr.build_parser().parse_args(["run"])
    assert a.home_ip == HOME and a.n == 7 and a.command == "run"
    with pytest.raises(SystemExit):
        gr.build_parser().parse_args([])                   # no command = argparse error, never a silent run

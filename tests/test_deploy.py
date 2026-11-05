"""The deployment files must not leak secrets and must keep the safety properties the runbook relies on."""
import os
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

DEPLOY = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "unraid"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def test_the_sampler_has_no_network_of_its_own():
    s = COMPOSE["services"]["sampler"]
    assert s["network_mode"] == "service:gluetun"
    assert "ports" not in s and "networks" not in s       # nothing can bypass the VPN container's network


def test_the_vpn_container_has_only_the_privileges_it_needs():
    g = COMPOSE["services"]["gluetun"]
    assert g["cap_add"] == ["NET_ADMIN"] and g["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert g["environment"]["VPN_SERVICE_PROVIDER"] == "protonvpn" and g["environment"]["VPN_TYPE"] == "wireguard"
    assert "privileged" not in g


def test_the_sampler_is_locked_down():
    s = COMPOSE["services"]["sampler"]
    assert s["read_only"] is True and s["cap_drop"] == ["ALL"] and "no-new-privileges:true" in s["security_opt"]
    assert s["volumes"] == ["./data:/data"]               # the only writable, persistent path


def test_neither_container_restarts_by_itself():
    assert all(svc.get("restart") == "no" for svc in COMPOSE["services"].values())


def test_secrets_come_from_the_environment_and_are_required():
    key = COMPOSE["services"]["gluetun"]["environment"]["WIREGUARD_PRIVATE_KEY"]
    home = COMPOSE["services"]["sampler"]["environment"]["HOME_IP"]
    assert key.startswith("${WIREGUARD_PRIVATE_KEY:?") and home.startswith("${HOME_IP:?")   # compose refuses to start without


def test_no_command_runs_by_default():
    text = (DEPLOY / "Dockerfile").read_text()
    assert "ENTRYPOINT" in text and not re.search(r"^\s*CMD\b", text, re.M)       # `up` must not start retailer traffic
    assert re.search(r"^USER\s+app", text, re.M)                                 # not root


def test_the_image_contains_only_the_scripts_it_needs():
    copies = [l for l in (DEPLOY / "Dockerfile").read_text().splitlines() if l.startswith("COPY")]
    assert len(copies) == 1 and all(f in copies[0] for f in (
        "spike_retailers.py", "spike_walmart_sellers.py", "guarded_run.py"))


def test_env_example_contains_placeholders_only():
    lines = [l for l in (DEPLOY / "env.example").read_text().splitlines() if l and not l.startswith("#")]
    values = dict(l.split("=", 1) for l in lines)
    assert values["WIREGUARD_PRIVATE_KEY"] == "CHANGE_ME" and values["HOME_IP"] == "CHANGE_ME"
    assert {"SERVER_COUNTRIES", "GLUETUN_IMAGE", "N_PAGES", "SEED"} <= set(values)


def test_no_key_or_ip_shaped_secret_anywhere_in_the_deploy_folder():
    key_re = re.compile(r"[A-Za-z0-9+/]{43}=")                # a WireGuard private/public key
    ip_re = re.compile(r"\b(?!127\.|0\.|10\.|192\.168\.)\d{1,3}(?:\.\d{1,3}){3}\b")
    for f in DEPLOY.rglob("*"):
        if f.is_file() and f.suffix in {".yml", ".md", ".example", ""} or f.name in {"Dockerfile", "env.example"}:
            text = f.read_text()
            assert not key_re.search(text), f"key-shaped string in {f.name}"
            assert not ip_re.search(text), f"IP-shaped string in {f.name}"


def test_local_state_and_secrets_are_git_ignored_and_not_in_the_image():
    root = DEPLOY.parents[1]
    ignore = (root / ".gitignore").read_text()
    docker_ignore = (root / ".dockerignore").read_text()
    assert ".env" in ignore and "deploy/unraid/data" in ignore
    assert "deploy/unraid/.env" in docker_ignore and "raw" in docker_ignore.split()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_file_is_valid_when_required_variables_are_set():
    env = dict(os.environ, WIREGUARD_PRIVATE_KEY="x", HOME_IP="81.2.69.142")
    r = subprocess.run(["docker", "compose", "-f", str(DEPLOY / "docker-compose.yml"), "config", "-q"],
                       env=env, capture_output=True, text=True)
    if "Cannot connect" in r.stderr or "unknown shorthand flag" in r.stderr or "is not a docker command" in r.stderr:
        pytest.skip("docker compose not usable here")
    assert r.returncode == 0, r.stderr


def _compose_config(**present):
    """`docker compose config -q` with exactly the given secrets set (all others removed from the environment)."""
    env = {k: v for k, v in os.environ.items() if k not in {"WIREGUARD_PRIVATE_KEY", "HOME_IP"}}
    env.update(present)
    return subprocess.run(["docker", "compose", "-f", str(DEPLOY / "docker-compose.yml"), "config", "-q"],
                          env=env, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
@pytest.mark.parametrize("present,missing", [
    ({"HOME_IP": "81.2.69.142"}, "WIREGUARD_PRIVATE_KEY"),
    ({"WIREGUARD_PRIVATE_KEY": "x"}, "HOME_IP"),
])
def test_compose_refuses_to_start_when_either_secret_is_missing(present, missing):
    # Compose reports one missing variable at a time, and which one it reports first when several are missing is not
    # something to rely on. So remove them one at a time and check the one that is missing is the one named.
    r = _compose_config(**present)
    if "is not a docker command" in r.stderr or "unknown shorthand flag" in r.stderr:
        pytest.skip("docker compose not usable here")
    assert r.returncode != 0 and missing in r.stderr, r.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_refuses_to_start_with_no_secrets_at_all():
    r = _compose_config()
    if "is not a docker command" in r.stderr or "unknown shorthand flag" in r.stderr:
        pytest.skip("docker compose not usable here")
    assert r.returncode != 0 and ("WIREGUARD_PRIVATE_KEY" in r.stderr or "HOME_IP" in r.stderr), r.stderr

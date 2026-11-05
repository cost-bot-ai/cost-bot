# Walmart seller check on Unraid, behind Proton VPN

Re-runs the Walmart sample from **your own network path** (a Proton VPN exit, not a datacenter), to learn whether
Walmart's bot protection treats it differently from the cloud session that was challenged on the third request.

**What it does:** reads up to 20 randomly chosen Walmart product pages, 5 seconds apart, and records for each
whether Walmart itself sells it (`INTERNAL`) or a marketplace seller does (`EXTERNAL`). About 25 to 30 requests in total (the pages, two `robots.txt` reads, two sitemap files the first time, and one IP check).

**What it does not do:** no scheduling, no loops, no retries, no search pages, no API calls, no store selection,
no browser disguise. If Walmart blocks or challenges it, it stops, records that, and **refuses to run again for
24 hours**. Do not work around that.

## Before you start

- [ ] **Read Proton VPN's terms on automated traffic.** I could not find them from the cloud session, so I have not
      checked that this use is allowed. You are the account holder.
- [ ] **Wait until at least 2026-11-06 03:00 UTC** before the Walmart run (step 8). The last challenge was about
      2026-11-05 02:20 UTC; the 24 hour back-off applies to us, not only to the script.
- [ ] Docker is available on the Unraid box and `docker compose version` works in its terminal. I have **not**
      verified how your Unraid version provides Compose; if the command is missing, install Unraid's Compose
      Manager plugin or a standalone Compose binary.
- [ ] You know your real public IP **with the VPN off** (your router's status page shows it).

Settings for the VPN container come from the gluetun project's own wiki (checked 2026-11-05):
`VPN_SERVICE_PROVIDER=protonvpn`, `VPN_TYPE=wireguard`, `WIREGUARD_PRIVATE_KEY`, `SERVER_COUNTRIES`. The project's
README states it has a "built in firewall kill switch that only allows traffic through the VPN, to the VPN server,
and to allowed LAN devices". That is its claim; **step 7 tests it** rather than trusting it.

## Steps

### 1. Get the code

```bash
cd /mnt/user/appdata && git clone https://github.com/cost-bot-ai/cost-bot.git pricememory
```

```bash
cd /mnt/user/appdata/pricememory/deploy/unraid
```

(Adjust the path if you keep app data elsewhere.)

### 2. Create your private settings file

```bash
cp env.example .env && chmod 600 .env
```

Edit `.env` (for example with `nano .env`) and set:

- `WIREGUARD_PRIVATE_KEY`: generate a WireGuard configuration at `account.proton.me`, VPN, WireGuard, and copy the
  `PrivateKey` value. Per the gluetun wiki the key works for all Proton servers.
- `HOME_IP`: your real public IP from the checklist.
- `SERVER_COUNTRIES`: leave it, or pick one country. **Choose once and never change it to get past a block.**

`.env` is git-ignored. **Never paste its contents into chat, an issue, or a commit.**

### 3. Build the sampler image

```bash
docker compose build sampler
```

### 4. Pull the VPN image and pin it

```bash
docker compose pull gluetun
```

```bash
docker image inspect --format '{{index .RepoDigests 0}}' qmcgaw/gluetun
```

Copy the `qmcgaw/gluetun@sha256:...` line into `GLUETUN_IMAGE` in `.env`, so a later image update cannot change
the firewall behaviour you are about to test.

### 5. Start only the VPN container

```bash
docker compose up -d gluetun
```

```bash
docker compose logs --tail 40 gluetun
```

Wait until the log shows the VPN is connected. (I have not verified gluetun's exact log wording.)

### 6. Check that traffic leaves through the VPN

```bash
docker compose run --rm sampler egress
```

Expected: `Egress OK: traffic leaves via a different public IP than HOME_IP`.
If it says `FAILED`, **stop**: do not continue to step 7 or 8, and do not edit the script to make it pass.

### 7. Kill-switch drill (do this before any retailer traffic)

Both parts must pass. "Pass" means the command **fails** and never prints `Egress OK`.

**7a. VPN container stopped**

```bash
docker compose stop gluetun
```

```bash
docker compose run --rm --no-deps sampler egress
```

Expected: an error (Compose cannot attach the sampler to a stopped VPN container, or the egress check fails).
If it prints `Egress OK`, **stop and tell me**: the sampler has a network path that bypasses the VPN.

```bash
docker compose up -d gluetun
```

**7b. VPN container running, tunnel down**

```bash
docker exec gluetun ip link
```

Find the WireGuard interface in the list (commonly `wg0`; I have not verified the name gluetun uses).

```bash
docker exec gluetun ip link set <that-interface> down
```

```bash
docker compose run --rm --no-deps sampler egress
```

Expected: `Egress check FAILED` (exit code 3). If it prints `Egress OK`, **stop and tell me**.

```bash
docker compose restart gluetun
```

Then repeat step 6 and confirm it is OK again.

### 8. Run the check (not before 2026-11-06 03:00 UTC)

```bash
docker compose run --rm sampler run
```

It re-checks the VPN, refuses if a recent block is on record, fetches two Walmart sitemap files the first time
(robots.txt is checked first), then reads up to 20 pages. Exit codes: `0` clean, `1` stopped (block, challenge or
robots), `2` configuration or state problem, `3` VPN check failed, `4` refused because of the cooldown.

### 9. Read and share the result

The script prints a summary and writes `data/out/walmart_sellers_<time>.json`. That file holds seller fields and
URL slugs only: **no IP addresses and no page HTML**. Share the printed `summary` and the file name, not `.env`.

How to read it:

- **Stopped by a challenge within the first few pages again:** Walmart's protection blocks this path too. Do not
  retry; the runner will refuse for 24 hours anyway. Walmart is then not collectable by this method.
- **All 20 pages read:** the counts show how many of a random sample are first party. With only 20 pages the
  estimate is rough (roughly plus or minus 20 to 25 percentage points), and one clean run does not show that daily
  collection would stay unblocked.

## Stop immediately, and do not retry or work around it, if

- the egress check says FAILED, or either kill-switch drill prints `Egress OK`;
- the run reports a challenge, a 403/429, or "robots.txt disallows";
- anything asks you to change country, rotate servers, change the User-Agent, or edit `data/state.json`.

## Clean up

```bash
docker compose down
```

Delete `.env` when you are finished, and remove the WireGuard configuration in your Proton account if you do not
want the key to stay valid.

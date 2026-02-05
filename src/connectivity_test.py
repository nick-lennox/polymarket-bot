"""
Connectivity test - run on your deployment host to verify
TSA.gov and Polymarket APIs are accessible from that IP.

Usage:
    python -m src.connectivity_test
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, ".")


async def test_tsa():
    import httpx

    print("=" * 50)
    print("TEST 1: TSA.gov")
    print("=" * 50)

    url = "https://www.tsa.gov/travel/passenger-volumes"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        start = time.time()
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        elapsed = time.time() - start

        print(f"  Status: {resp.status_code}")
        print(f"  Latency: {elapsed:.2f}s")
        print(f"  Content-Length: {len(resp.text)} chars")

        if resp.status_code == 200:
            has_data = "passenger" in resp.text.lower() or "throughput" in resp.text.lower()
            print(f"  Contains passenger data: {has_data}")
            if has_data:
                print("  [PASS] TSA.gov is accessible")
                return True
            else:
                print("  [WARN] Page loaded but may be a captcha/block page")
                print(f"  Preview: {resp.text[:500]}")
                return False
        elif resp.status_code == 403:
            print("  [FAIL] 403 Forbidden - IP is blocked")
            return False
        elif resp.status_code == 429:
            print("  [FAIL] 429 Rate Limited")
            return False
        else:
            print("  [FAIL] Unexpected status code")
            return False
    except Exception as e:
        print(f"  [FAIL] Connection error: {e}")
        return False


async def test_polymarket_gamma():
    import httpx

    print()
    print("=" * 50)
    print("TEST 2: Polymarket Gamma API")
    print("=" * 50)

    url = "https://gamma-api.polymarket.com/events"
    params = {"limit": 1, "closed": "false"}

    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        elapsed = time.time() - start

        print(f"  Status: {resp.status_code}")
        print(f"  Latency: {elapsed:.2f}s")

        if resp.status_code == 200:
            data = resp.json()
            print(f"  Events returned: {len(data)}")
            if data:
                title = data[0].get("title", "N/A")[:60]
                print(f"  Sample event: {title}")
            print("  [PASS] Gamma API is accessible")
            return True
        elif resp.status_code == 403:
            print("  [FAIL] 403 Forbidden - IP blocked or geo-restricted")
            return False
        else:
            print(f"  [FAIL] Unexpected status: {resp.status_code}")
            return False
    except Exception as e:
        print(f"  [FAIL] Connection error: {e}")
        return False


async def test_polymarket_clob():
    import httpx

    print()
    print("=" * 50)
    print("TEST 3: Polymarket CLOB API")
    print("=" * 50)

    url = "https://clob.polymarket.com/time"

    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        elapsed = time.time() - start

        print(f"  Status: {resp.status_code}")
        print(f"  Latency: {elapsed:.2f}s")

        if resp.status_code == 200:
            print(f"  Server time: {resp.text.strip()}")
            print("  [PASS] CLOB API is accessible")
            return True
        elif resp.status_code == 403:
            print("  [FAIL] 403 Forbidden - IP blocked or geo-restricted")
            return False
        else:
            print(f"  [FAIL] Unexpected status: {resp.status_code}")
            return False
    except Exception as e:
        print(f"  [FAIL] Connection error: {e}")
        return False


async def test_polymarket_auth():
    print()
    print("=" * 50)
    print("TEST 4: Polymarket Authenticated Connection")
    print("=" * 50)

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = os.environ.get("POLYMARKET_FUNDER", "")

    if not pk:
        print("  [SKIP] POLYMARKET_PRIVATE_KEY not set")
        return None

    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk if pk.startswith("0x") else f"0x{pk}",
            chain_id=137,
            funder=funder or None,
        )

        start = time.time()
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        elapsed = time.time() - start

        api_key_preview = creds.api_key[:15]
        print(f"  Auth latency: {elapsed:.2f}s")
        print(f"  API key derived: {api_key_preview}...")
        print("  [PASS] Authenticated successfully")
        return True
    except Exception as e:
        err_str = str(e)
        print(f"  [FAIL] Auth error: {err_str}")
        if "403" in err_str or "forbidden" in err_str.lower():
            print("  Likely geo-restricted or IP blocked")
        return False


async def check_ip():
    import httpx

    print()
    print("=" * 50)
    print("OUTBOUND IP CHECK")
    print("=" * 50)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            ip_data = resp.json()
            ip = ip_data.get("ip", "unknown")
            print(f"  Your outbound IP: {ip}")

            resp2 = await client.get(f"https://ipapi.co/{ip}/json/")
            if resp2.status_code == 200:
                geo = resp2.json()
                country = geo.get("country_name", "?")
                region = geo.get("region", "?")
                org = geo.get("org", "?")
                cc = geo.get("country_code", "")
                print(f"  Country: {country}")
                print(f"  Region: {region}")
                print(f"  ISP: {org}")
                if cc != "US":
                    print("  [WARN] Non-US IP - Polymarket may geo-restrict")
    except Exception as e:
        print(f"  Could not determine IP: {e}")


async def main():
    print()
    print("TSA POLYMARKET BOT - CONNECTIVITY TEST")
    print("Run this on your deployment host to verify access")
    print()

    await check_ip()

    tsa_ok = await test_tsa()
    gamma_ok = await test_polymarket_gamma()
    clob_ok = await test_polymarket_clob()
    auth_ok = await test_polymarket_auth()

    print()
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    results = {
        "TSA.gov scraping": tsa_ok,
        "Gamma API (market discovery)": gamma_ok,
        "CLOB API (order books/trading)": clob_ok,
        "CLOB authenticated": auth_ok,
    }
    for name, ok in results.items():
        if ok is None:
            status = "[SKIP]"
        elif ok:
            status = "[PASS]"
        else:
            status = "[FAIL]"
        print(f"  {status} {name}")

    any_fail = any(v is False for v in results.values())
    if any_fail:
        print()
        print("REMEDIATION OPTIONS:")
        print("  1. TSA blocks: add rotating User-Agent or use residential proxy")
        print("  2. Polymarket geo-blocks: need US-based VPN/proxy or US server")
        print("  3. CLOB auth fails: verify POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER")
    else:
        print()
        print("  All checks passed - good to go!")

    print()


if __name__ == "__main__":
    asyncio.run(main())

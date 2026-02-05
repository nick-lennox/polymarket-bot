"""Pre-flight verification. Run: python -m src.preflight"""
import sys, inspect, asyncio

def main():
    errors, warnings = [], []
    p = print
    p("=" * 60)
    p("  PRE-FLIGHT VERIFICATION")
    p("=" * 60)
    p()
    p("--- STEP 1: Imports ---")
    try:
        from src.config import Settings
        from src.tsa_scraper import TSAScraper, TSADataPoint, DEFAULT_HEADERS
        from src.polymarket import PolymarketClient, MarketOutcome
        from src.trading import TradingEngine, get_polymarket_bracket
        from src.main import TradingBot
        p("[OK] All imports")
    except Exception as e:
        p(f"[FAIL] {e}"); sys.exit(1)

    p()
    p("--- STEP 2: Code Paths ---")
    s = inspect.getsource(PolymarketClient.discover_tsa_market)
    if "number-of-tsa-passengers" in s: p("[OK] slug construction")
    else: errors.append("no slug construction")
    if "text search" in s: errors.append("old search code")
    else: p("[OK] no old search")
    s = inspect.getsource(TradingBot._get_poll_interval)
    if "return 1" in s: p("[OK] 1s hot window")
    elif "return 3" in s: warnings.append("3s not 1s")
    if "time(8, 0)" in s and "time(9, 30)" in s: p("[OK] 8:00-9:30 ET")
    else: errors.append("window times")
    if hasattr(TSAScraper, "fetch_if_changed"):
        s = inspect.getsource(TSAScraper.fetch_if_changed)
        if "If-Modified-Since" in s and "304" in s: p("[OK] conditional GET")
        else: errors.append("conditional GET broken")
    else: errors.append("no fetch_if_changed")
    s = inspect.getsource(TSAScraper.check_for_new_data)
    if "fetch_if_changed" in s: p("[OK] lightweight polling")
    else: errors.append("not using fetch_if_changed")
    if hasattr(TradingEngine, "_analyze_wrong_outcome"): p("[OK] BUY_NO")
    else: errors.append("no BUY_NO")
    s = inspect.getsource(TradingEngine.execute_signals)
    if "BUY_YES" in s and "no_token_id" in s: p("[OK] YES/NO routing")
    else: errors.append("token routing")
    s = inspect.getsource(TradingEngine._brackets_match)
    if "o_has_range" in s: p("[OK] strict matching")
    else: errors.append("bracket matching")
    if "no-cache" in DEFAULT_HEADERS.get("Cache-Control", ""): p("[OK] cache-bust")
    else: errors.append("no cache-bust")

    p()
    p("--- STEP 3: Bracket Tests ---")
    class FC:
        max_trade_size_usd=50; max_buy_price=0.95; min_edge=0.05; dry_run=True
    eng = TradingEngine(None, FC())
    cases = [("1.5M - 1.7M","1.5M-1.7M",True),("1.7M-1.9M","1.7M-1.9M",True),
        ("<1.5M","<1.5M",True),(">2.3M",">2.3M",True),
        ("Under 1.5M","<1.5M",True),("Over 2.3M",">2.3M",True),
        ("2.1M - 2.3M",">2.3M",False),(">2.3M","2.1M-2.3M",False),
        ("<1.5M","1.5M-1.7M",False),("1.5M-1.7M","1.7M-1.9M",False)]
    ok = all(eng._brackets_match(o,b)==e for o,b,e in cases)
    p(f"[OK] {len(cases)} bracket tests" if ok else "[FAIL] bracket tests")
    if not ok: errors.append("bracket tests")
    bt = [(1400000,"<1.5M"),(1600000,"1.5M-1.7M"),(1800000,"1.7M-1.9M"),
        (2000000,"1.9M-2.1M"),(2200000,"2.1M-2.3M"),(2400000,">2.3M")]
    ok = all(get_polymarket_bracket(c)==e for c,e in bt)
    p(f"[OK] {len(bt)} assignments" if ok else "[FAIL] assignments")
    if not ok: errors.append("bracket assignments")

    p()
    p("--- STEP 4: Config ---")
    st = Settings(polymarket_private_key="", target_market_slug="")
    if st.dry_run: p("[OK] DY_RUN=True")
    else: errors.append("DRY_RUN=False!")
    p(f"  budget=${st.max_trade_size_usd} max_price={st.max_buy_price} edge={st.min_edge}")

    p()
    p("--- STEP 5: Timezone ---")
    import pytz
    from datetime import datetime, time as dtime, date, timedelta
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    hms = str(now.hour) + ":" + str(now.minute).zfill(2) + ":" + str(now.second).zfill(2)
    p(f"[OK] ET={hms}  weekday={now.weekday()<5}  hot={dtime(8,0)<=now.time()<=dtime(9,30)}")

    p()
    p("--- STEP 6: Connectivity ---")
    async def test_conn():
        async with TSAScraper() as sc:
            html = await sc.fetch_page()
            data = sc.parse_html(html)
            if data:
                d = data[0]
                p(f"[OK] TSA.gov: {d.date} - {d.formatted_count}")
            else: errors.append("TSA: no data")
            html2 = await sc.fetch_if_changed()
            if html2 is None: p("[OK] Conditional GET: 304 (0 bytes)")
            else: p(f"[OK] Conditional GET: {len(html2)} bytes")
    asyncio.run(test_conn())

    import httpx
    try:
        r = httpx.get("https://gamma-api.polymarket.com/events", params={"slug": "test"}, timeout=15.0)
        p(f"[OK] Gamma API ({r.status_code})")
    except Exception as e: errors.append(f"Gamma: {e}")

    p()
    p("--- STEP 7: Auto-Discovery ---")
    for off, lbl in [(0,"Today"),(1,"Tomorrow")]:
        d = date.today() + timedelta(days=off)
        mn = d.strftime("%B").lower()
        sl = f"number-of-tsa-passengers-{mn}-{d.day}"
        try:
            r = httpx.get("https://gamma-api.polymarket.com/events", params={"slug": sl}, timeout=15.0)
            ev = r.json()
            if ev: p(f"[OK] {lbl}: {sl}")
            else:
                warnings.append(f"{lbl}: {sl} not found")
                p(f"[WARN] {lbl}: {sl} not found")
        except Exception as e: warnings.append(f"{lbl}: {e}")

    p()
    p("=" * 60)
    p("  RESULTS")
    p("=" * 60)
    if errors:
        p()
        p("ERRORS:")
        for e in errors: p(f"  [X] {e}")
    if warnings:
        p()
        p("WARNINGS:")
        for w in warnings: p(f"  [!] {w}")
    if not errors and not warnings:
        p()
        p("  ALL CHECKS PASSED. Ready for live.")
    elif not errors:
        p()
        p("  No errors. Review warnings.")
    p("")
    sys.exit(1 if errors else 0)

if __name__ == "__main__":
    main()

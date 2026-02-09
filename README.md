# TSA Polymarket Trading Bot

Automated trading bot for TSA daily passenger count bracket markets on Polymarket.

## Two Trading Strategies

### 1. Data Racing (Original) - `src.main`
Monitors [tsa.gov](https://www.tsa.gov/travel/passenger-volumes) for new data releases. When new data appears (~8:20 AM ET), identifies the correct bracket and trades before the market prices it in.

**Problem**: Smart money is faster. By the time we detect the data, markets are already at 99%+.

### 2. Movement Detection (New) - `src.movement_bot`
Instead of racing to detect data, follow the smart money. Monitors order book movements during the release window (7-10 AM ET) and detects statistically significant price spikes using z-scores.

**Strategy**:
- At 7 AM ET, snapshot baseline prices for all outcomes
- Poll order books every 1.5 seconds
- Calculate z-score: `(current_price - baseline) / std_dev`
- When z-score exceeds threshold (default 2.5), trigger buy signal
- Scale into position: 50% → 30% → 20% of budget on successive triggers

## Quick Start

### 1. Clone and Configure

```bash
cd tsa-polymarket-bot
cp .env.example .env
# Edit .env with your credentials
```

### 2. Run with Docker

```bash
# Movement Detection Strategy (recommended)
docker-compose build
docker-compose run --rm tsa-bot python -m src.movement_bot

# Original Data Racing Strategy
docker-compose up -d
```

### 3. Run Locally

```bash
pip install -r requirements.txt

# Movement Detection
python -m src.movement_bot

# Original Data Racing
python -m src.main
```

## Pre-Flight Verification

Before going live, run the pre-flight check:

```bash
docker build -t tsa-bot .
docker run --rm tsa-bot python -m src.preflight
```

This verifies imports, code paths, bracket matching, config safety, timezone support, and API connectivity.

## Configuration

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYMARKET_PRIVATE_KEY` | (required) | Ethereum private key for trading |
| `POLYMARKET_FUNDER` | (optional) | Proxy wallet funder address |
| `TARGET_MARKET_SLUG` | (auto) | Event slug. If empty, auto-discovers today's market |
| `MAX_TRADE_SIZE_USD` | 50 | Total USD budget per session |
| `MAX_BUY_PRICE` | 0.95 | Max price to pay for tokens |
| `MIN_EDGE` | 0.05 | Minimum edge required (data racing only) |
| `DRY_RUN` | true | Log trades without executing |
| `LOG_LEVEL` | INFO | Logging verbosity |

### Movement Detection Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ZSCORE_THRESHOLD` | 2.5 | Z-score to trigger a buy signal |
| `SCALE_IN_PCTS` | 50,30,20 | Budget % per successive trigger |
| `MIN_PRICE_CHANGE` | 0.05 | Min price move to trigger (prevents noise) |
| `MONITOR_WINDOW_START_HOUR` | 7 | Start of monitoring window (ET) |
| `MONITOR_WINDOW_END_HOUR` | 10 | End of monitoring window (ET) |

## Architecture

```
src/
├── movement_bot.py      # NEW: Movement detection entry point
├── movement_detector.py # NEW: Z-score based signal generation
├── main.py              # Original: Data racing entry point
├── tsa_scraper.py       # TSA website monitoring
├── polymarket.py        # Polymarket CLOB + Gamma API wrapper
├── trading.py           # Bracket matching, trade execution
├── config.py            # Pydantic configuration
├── simulate.py          # End-to-end simulation
├── preflight.py         # Pre-flight verification
└── connectivity_test.py # Network diagnostics
```

### Movement Detection Flow

```
7:00 AM ET - Window Opens:
  ├─ Auto-discover today's TSA market
  ├─ Snapshot baseline prices for all outcomes
  └─ Reset budget to MAX_TRADE_SIZE_USD

Every 1.5 seconds:
  ├─ Fetch order books for all outcomes
  ├─ Calculate z-score for each outcome
  │     z = (current_price - baseline) / std_dev
  │
  ├─ If z-score > ZSCORE_THRESHOLD and price < MAX_BUY_PRICE:
  │     ├─ Log SIGNAL
  │     ├─ Allocate budget portion (50%, then 30%, then 20%)
  │     └─ Execute BUY market order (if not DRY_RUN)
  └─ Continue until window ends or budget exhausted

10:00 AM ET - Window Closes:
  └─ Log session summary
```

### Original Data Racing Flow

```
Every 1-30 seconds (1s during 8:00-9:30 AM ET):
  ├─ Conditional GET to TSA.gov
  │     ├─ 304 Not Modified → skip (0 bytes)
  │     └─ 200 OK → parse HTML
  │
  ├─ New date detected?
  │     ├─ Map passenger count to bracket
  │     ├─ BUY YES on correct outcome
  │     ├─ BUY NO on wrong outcomes (where cheap)
  │     └─ Rank by edge, fill from budget
  └─ Continue
```

## TSA Market Brackets

| Bracket | Passenger Count |
|---------|----------------|
| <1.5M | Under 1,500,000 |
| 1.5M-1.7M | 1,500,000 - 1,699,999 |
| 1.7M-1.9M | 1,700,000 - 1,899,999 |
| 1.9M-2.1M | 1,900,000 - 2,099,999 |
| 2.1M-2.3M | 2,100,000 - 2,299,999 |
| >2.3M | 2,300,000+ |

## Risk Controls

- **Dry Run Mode**: Always start with `DRY_RUN=true`
- **Budget Cap**: `MAX_TRADE_SIZE_USD` limits total spend
- **Max Buy Price**: Won't overpay (default: $0.95)
- **Z-Score Threshold**: Higher = fewer but more confident signals
- **Scale-In**: Don't commit entire budget on first signal
- **Pre-Flight Check**: Verifies code and connectivity before deployment

## Testing

```bash
# Test TSA scraper
python -m src.tsa_scraper

# Test network connectivity
python -m src.connectivity_test

# Run simulation
python -m src.simulate

# Pre-flight check (in Docker)
docker run --rm tsa-bot python -m src.preflight
```

## Disclaimers

- **Not Financial Advice**: Experimental software for educational purposes
- **Risk of Loss**: Trading involves risk of losing your entire investment
- **No Guarantees**: Past performance does not guarantee future results
- **Your Responsibility**: Verify all trades and monitor carefully

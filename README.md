# TSA Polymarket Trading Bot

Automated trading bot that monitors TSA daily passenger count data and trades bracket outcomes on Polymarket. When new data is published on [tsa.gov](https://www.tsa.gov/travel/passenger-volumes), the bot identifies the correct bracket, buys YES on the correct outcome and NO on wrong outcomes, and executes trades before the market fully prices in the information.

## Strategy

1. **Monitor** TSA checkpoint passenger volumes (updated weekdays ~8:20 AM ET)
2. **Detect** new data within seconds using conditional GET polling (1s during hot window)
3. **Identify** the correct bracket (e.g., "1.7M-1.9M") from the passenger count
4. **Trade** BUY YES on the correct outcome + BUY NO on wrong outcomes where cheap
5. **Budget** all trades from a single pool, ranked by edge (best opportunities first)

## Quick Start

### 1. Clone and Configure

```bash
cd tsa-polymarket-bot
cp .env.example .env
# Edit .env with your credentials
```

### 2. Run with Docker

```bash
docker-compose build
docker-compose up -d
docker-compose logs -f
```

### 3. Run Locally

```bash
pip install -r requirements.txt
python -m src.main
```

## Pre-Flight Verification

Before going live, run the pre-flight check to verify everything works inside the Docker container:

```bash
docker build -t tsa-bot .
docker run --rm tsa-bot python -m src.preflight
```

This tests: imports, critical code paths, bracket matching, config safety, timezone support, live connectivity (TSA.gov, Gamma API), conditional GET, and auto-discovery for today/tomorrow's markets.

## Simulation

Test the full pipeline without trading:

```bash
python -m src.simulate

# Override parameters:
MAX_TRADE_SIZE_USD=100 MIN_EDGE=0.03 python -m src.simulate
```

The simulator fetches live TSA data and Polymarket order books, generates signals, and shows projected profit for each trade.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYMARKET_PRIVATE_KEY` | (required) | Ethereum private key for trading |
| `POLYMARKET_FUNDER` | (optional) | Proxy wallet funder address |
| `TARGET_MARKET_SLUG` | (auto) | Event slug. If empty, auto-discovers today's market |
| `MAX_TRADE_SIZE_USD` | 50 | Total USD budget per data release (across all trades) |
| `MAX_BUY_PRICE` | 0.95 | Max price to pay for YES/NO tokens |
| `MIN_EDGE` | 0.05 | Minimum edge required to trade |
| `DRY_RUN` | true | Log trades without executing |
| `POLL_INTERVAL_SECONDS` | 30 | Default polling frequency (seconds) |
| `LOG_LEVEL` | INFO | Logging verbosity |

## Architecture

```
src/
  main.py              # Entry point, orchestration loop, dynamic polling
  tsa_scraper.py       # TSA website monitoring with conditional GET
  polymarket.py        # Polymarket CLOB API + Gamma API wrapper
  trading.py           # Trading logic, bracket matching, signal generation
  config.py            # Pydantic configuration management
  simulate.py          # End-to-end simulation with profit projections
  preflight.py         # Pre-flight verification for Docker deployment
  connectivity_test.py # Network connectivity diagnostics
```

### Components

**TSA Scraper** (`tsa_scraper.py`)
- Polls TSA website for new passenger data
- Cache-busting headers to bypass Akamai CDN (10-min TTL)
- `If-Modified-Since` conditional GET for lightweight polling (~0 bytes on 304)
- During hot window (8:00-9:30 AM ET), polls every 1 second

**Polymarket Client** (`polymarket.py`)
- Connects to CLOB API on Polygon via `py-clob-client`
- Fetches order books for both YES and NO tokens
- Auto-discovers daily TSA markets by constructing slugs (`number-of-tsa-passengers-{month}-{day}`)
- Submits FOK (fill-or-kill) market orders

**Trading Engine** (`trading.py`)
- Maps passenger counts to Polymarket brackets
- Strict bracket matching to prevent false positives
- Generates BUY YES signals on correct outcome
- Generates BUY NO signals on wrong outcomes where NO is cheap
- Ranks all opportunities by edge, fills best-first from budget pool

**Main Loop** (`main.py`)
- Dynamic polling: 1s during hot window (8:00-9:30 AM ET weekdays), configurable otherwise
- Auto-discovers market slug if `TARGET_MARKET_SLUG` is not set
- Graceful shutdown on SIGINT/SIGTERM

## How It Works

```
Every 1-30 seconds (depending on time):
  │
  ├─ fetch_if_changed() ─── 304 Not Modified? ─── skip (0 bytes)
  │                              │
  │                         200 OK (new content)
  │                              │
  ├─ Parse HTML table ─── New date detected?
  │                              │
  │                         Yes: trigger trading
  │                              │
  ├─ Auto-discover market slug (if not configured)
  ├─ Fetch order books (YES + NO for each outcome)
  ├─ Analyze: BUY YES on correct bracket
  ├─ Analyze: BUY NO on wrong brackets (where cheap)
  ├─ Rank all signals by edge (highest first)
  └─ Execute trades from $MAX_TRADE_SIZE_USD budget
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

- **Dry Run Mode**: Always start with `DRY_RUN=true` to verify behavior
- **Single Budget Pool**: `MAX_TRADE_SIZE_USD` caps total spend per data release
- **Max Buy Price**: Won't overpay for tokens (default: $0.95)
- **Min Edge**: Requires minimum profit margin to trade (default: 5%)
- **Strict Bracket Matching**: Prevents false-positive matches between similar brackets
- **Pre-Flight Check**: Verifies all code paths and connectivity before deployment

## Testing

```bash
# Test TSA scraper (fetches live data + tests conditional GET)
python -m src.tsa_scraper

# Test network connectivity
python -m src.connectivity_test

# Run full simulation
python -m src.simulate

# Pre-flight verification (run inside Docker)
docker run --rm tsa-bot python -m src.preflight
```

## Docker Deployment

```bash
# Build
docker-compose build

# Run (detached)
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

For Portainer: point the stack at this repo, configure environment variables, and deploy. Run the pre-flight check after each deployment to verify the container is ready.

## Disclaimers

- **Not Financial Advice**: This is experimental software for educational purposes
- **Risk of Loss**: Trading involves risk of losing your entire investment
- **No Guarantees**: Past performance does not guarantee future results
- **Your Responsibility**: Verify all trades and monitor the bot carefully

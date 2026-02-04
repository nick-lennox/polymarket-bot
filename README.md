# TSA Polymarket Trading Bot

Automated trading bot that reacts to TSA daily passenger count data releases on Polymarket.

## Strategy

The TSA publishes daily checkpoint passenger numbers at https://www.tsa.gov/travel/passenger-volumes. Data is typically updated Monday-Friday by 9am ET. This bot monitors for new data releases and immediately trades on Polymarket TSA passenger count markets when new data appears.

## Quick Start

### 1. Clone and Setup

```bash
cd tsa-polymarket-bot
cp .env.example .env
# Edit .env with your credentials
```

### 2. Configure

Edit `.env` with your settings:

```env
# Required: Your Polygon wallet private key
POLYMARKET_PRIVATE_KEY=0x...

# Required: The market condition ID to trade
TARGET_MARKET_SLUG=...

# Start in dry-run mode (recommended)
DRY_RUN=true
```

### 3. Run with Docker

```bash
docker-compose up -d
docker-compose logs -f
```

### 4. Run Locally

```bash
pip install -r requirements.txt
python -m src.main
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| POLYMARKET_PRIVATE_KEY | (required) | Ethereum private key for trading |
| POLYMARKET_FUNDER | (optional) | Proxy wallet funder address |
| TARGET_MARKET_SLUG | (required) | Market condition ID to trade |
| MAX_TRADE_SIZE_USD | 50 | Maximum USD per trade |
| MAX_BUY_PRICE | 0.95 | Max price to pay for YES tokens |
| MIN_EDGE | 0.05 | Minimum edge required to trade |
| DRY_RUN | true | Log trades without executing |
| POLL_INTERVAL_SECONDS | 30 | Polling frequency |
| LOG_LEVEL | INFO | Logging verbosity |

## Architecture

```
src/
  main.py          # Entry point, orchestration loop
  tsa_scraper.py   # TSA website monitoring and parsing
  polymarket.py    # Polymarket CLOB API wrapper
  trading.py       # Trading logic and decision engine
  config.py        # Configuration management
```

### Components

1. **TSA Scraper** (tsa_scraper.py)
   - Polls TSA website for new passenger data
   - Parses HTML table to extract date and count
   - Detects when new data appears

2. **Polymarket Client** (polymarket.py)
   - Connects to CLOB API on Polygon
   - Fetches order books
   - Submits market orders

3. **Trading Engine** (trading.py)
   - Maps passenger counts to market brackets
   - Identifies mispriced opportunities
   - Generates trade signals with risk controls

4. **Main Loop** (main.py)
   - Orchestrates monitoring and trading
   - Handles graceful shutdown
   - Manages state and logging

## Testing

### Test TSA Scraper

```bash
cd tsa-polymarket-bot
python -c "import asyncio; import sys; sys.path.insert(0,'.'); from src.tsa_scraper import test_scraper; asyncio.run(test_scraper())"
```

## Docker Deployment

### Build and Run

```bash
docker-compose build
docker-compose up -d
```

### View Logs

```bash
docker-compose logs -f
```

### Stop

```bash
docker-compose down
```

## Risk Controls

- **Dry Run Mode**: Always start with DRY_RUN=true
- **Max Trade Size**: Limits per-trade exposure
- **Max Buy Price**: Won't overpay for outcomes
- **Min Edge**: Requires minimum profit potential
- **Single Market Focus**: Only trades configured market

## How It Works

1. Bot polls TSA website every N seconds
2. When new data appears (new date in table):
   - Parse the actual passenger count
   - Determine which bracket it falls into (e.g., "2.8M - 2.9M")
   - Fetch Polymarket order book for that market
   - If correct outcome is trading below threshold, buy YES
3. Log all activity and trade results

## Disclaimers

- **Not Financial Advice**: This is experimental software for educational purposes
- **Risk of Loss**: Trading involves risk of losing your entire investment
- **No Guarantees**: Past performance does not guarantee future results
- **Your Responsibility**: Verify all trades and monitor the bot carefully

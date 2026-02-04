# Deploying to Portainer

## Method 1: Docker Compose Stack (Easiest)

### Step 1: Copy files to your Portainer host
```bash
scp -r tsa-polymarket-bot/ user@your-server:/opt/stacks/
```

### Step 2: In Portainer UI
1. Go to **Stacks** → **Add Stack**
2. Name: `tsa-polymarket-bot`
3. Select **Upload** and upload docker-compose.yml
   OR select **Web editor** and paste:

```yaml
version: '3.8'

services:
  tsa-polymarket-bot:
    build: .
    container_name: tsa-polymarket-bot
    restart: unless-stopped
    environment:
      - PYTHONUNBUFFERED=1
      - POLYMARKET_PRIVATE_KEY=${POLYMARKET_PRIVATE_KEY}
      - POLYMARKET_FUNDER=${POLYMARKET_FUNDER}
      - TARGET_MARKET_SLUG=${TARGET_MARKET_SLUG}
      - MAX_TRADE_SIZE_USD=${MAX_TRADE_SIZE_USD:-50}
      - MAX_BUY_PRICE=${MAX_BUY_PRICE:-0.95}
      - MIN_EDGE=${MIN_EDGE:-0.05}
      - DRY_RUN=${DRY_RUN:-true}
      - POLL_INTERVAL_SECONDS=${POLL_INTERVAL_SECONDS:-30}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

4. Scroll down to **Environment variables** and add:
   - POLYMARKET_PRIVATE_KEY = (your key)
   - POLYMARKET_FUNDER = (your proxy address)  
   - TARGET_MARKET_SLUG = number-of-tsa-passengers-february-4
   - DRY_RUN = true

5. Click **Deploy the stack**

---

## Method 2: Pre-built Image

### Step 1: Build and push to Docker Hub (or your registry)
```bash
cd tsa-polymarket-bot
docker build -t yourusername/tsa-polymarket-bot:latest .
docker push yourusername/tsa-polymarket-bot:latest
```

### Step 2: In Portainer, create stack with:
```yaml
version: '3.8'

services:
  tsa-polymarket-bot:
    image: yourusername/tsa-polymarket-bot:latest
    container_name: tsa-polymarket-bot
    restart: unless-stopped
    environment:
      - POLYMARKET_PRIVATE_KEY=${POLYMARKET_PRIVATE_KEY}
      - POLYMARKET_FUNDER=${POLYMARKET_FUNDER}
      - TARGET_MARKET_SLUG=${TARGET_MARKET_SLUG}
      - DRY_RUN=${DRY_RUN:-true}
      - POLL_INTERVAL_SECONDS=${POLL_INTERVAL_SECONDS:-30}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
```

---

## Method 3: Git Repository

1. Push code to GitHub
2. In Portainer: **Stacks** → **Add Stack** → **Repository**
3. Enter repo URL
4. Set path to docker-compose.yml
5. Add environment variables
6. Deploy

---

## Viewing Logs in Portainer

1. Go to **Containers**
2. Click on `tsa-polymarket-bot`
3. Click **Logs** tab
4. Enable **Auto-refresh** to watch live


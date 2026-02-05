"""
TSA Passenger Volume Scraper

Monitors https://www.tsa.gov/travel/passenger-volumes for new daily passenger counts.
Data is typically updated Monday-Friday by ~8:20am ET.

Optimizations:
- Cache-busting query params to bypass Akamai CDN (10-min TTL)
- If-Modified-Since conditional GET for lightweight polling (0 bytes on 304)
- During hot window, uses conditional requests to poll aggressively
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional
import re
import time

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TSA_URL = "https://www.tsa.gov/travel/passenger-volumes"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}


@dataclass
class TSADataPoint:
    """Represents a single day TSA passenger count."""
    date: date
    passenger_count: int
    year_ago_count: Optional[int] = None

    @property
    def formatted_count(self) -> str:
        return f"{self.passenger_count:,}"

    @property
    def millions(self) -> float:
        return self.passenger_count / 1_000_000

    def get_bracket(self, bracket_size: float = 0.1) -> str:
        millions = self.millions
        lower = (int(millions / bracket_size) * bracket_size)
        upper = lower + bracket_size
        return f"{lower:.1f}M - {upper:.1f}M"


class TSAScraper:
    """Scrapes TSA passenger volume data.

    Uses If-Modified-Since conditional GET for lightweight polling.
    When the server returns 304 (Not Modified), no body is transferred (~0 bytes).
    Only when content actually changes do we download the full ~150KB page.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._last_known_date: Optional[date] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._last_modified: Optional[str] = None  # Last-Modified header from server
        self._etag: Optional[str] = None  # ETag header from server
        self._conditional_hits: int = 0  # 304 responses (no change)
        self._conditional_misses: int = 0  # 200 responses (content changed)

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=self.timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    @property
    def last_known_date(self) -> Optional[date]:
        return self._last_known_date

    @last_known_date.setter
    def last_known_date(self, value: date):
        self._last_known_date = value

    @property
    def conditional_stats(self) -> str:
        total = self._conditional_hits + self._conditional_misses
        if total == 0:
            return "no conditional requests yet"
        hit_rate = self._conditional_hits / total * 100
        return f"{self._conditional_hits} hits / {self._conditional_misses} misses ({hit_rate:.0f}% cache hit rate)"

    async def fetch_page(self) -> str:
        if not self._client:
            raise RuntimeError("Scraper must be used as async context manager")
        # Cache-bust with timestamp to bypass CDN/Akamai 10-min TTL
        cache_buster = int(time.time() * 1000)
        url = f"{TSA_URL}?_={cache_buster}"
        logger.debug(f"Fetching {url}")
        response = await self._client.get(url)
        response.raise_for_status()

        # Store conditional headers for future lightweight requests
        if "Last-Modified" in response.headers:
            self._last_modified = response.headers["Last-Modified"]
            logger.debug(f"Stored Last-Modified: {self._last_modified}")
        if "ETag" in response.headers:
            self._etag = response.headers["ETag"]
            logger.debug(f"Stored ETag: {self._etag}")

        return response.text


    async def fetch_if_changed(self) -> Optional[str]:
        """Lightweight conditional fetch - returns None if content unchanged.

        Uses If-Modified-Since and If-None-Match headers. The server returns:
        - 304 Not Modified (0 bytes) if content hasn't changed
        - 200 OK with full page if content has changed

        This saves ~150KB per request when polling every few seconds.
        Falls back to full fetch if no conditional headers are stored yet.
        """
        if not self._client:
            raise RuntimeError("Scraper must be used as async context manager")

        # If we don't have conditional headers yet, do a full fetch
        if not self._last_modified and not self._etag:
            logger.debug("No conditional headers yet, doing full fetch")
            return await self.fetch_page()

        # Build conditional request headers
        cache_buster = int(time.time() * 1000)
        url = f"{TSA_URL}?_={cache_buster}"
        conditional_headers = {}
        if self._last_modified:
            conditional_headers["If-Modified-Since"] = self._last_modified
        if self._etag:
            conditional_headers["If-None-Match"] = self._etag

        logger.debug(f"Conditional fetch: If-Modified-Since={self._last_modified}")
        response = await self._client.get(url, headers=conditional_headers)

        if response.status_code == 304:
            self._conditional_hits += 1
            logger.debug(f"304 Not Modified (saved ~150KB) [{self.conditional_stats}]")
            return None

        # Content changed - we got a 200 with the full page
        response.raise_for_status()
        self._conditional_misses += 1
        logger.info(f"Content changed! (200 OK, {len(response.text)} bytes) [{self.conditional_stats}]")

        # Update conditional headers for next request
        if "Last-Modified" in response.headers:
            self._last_modified = response.headers["Last-Modified"]
        if "ETag" in response.headers:
            self._etag = response.headers["ETag"]

        return response.text

    def parse_html(self, html: str) -> list[TSADataPoint]:
        soup = BeautifulSoup(html, "html.parser")
        data_points = []
        table = soup.find("table")
        if not table:
            logger.warning("No table found in TSA page")
            return []
        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            try:
                date_text = cells[0].get_text(strip=True)
                parsed_date = self._parse_date(date_text)
                if not parsed_date:
                    continue
                count_text = cells[1].get_text(strip=True)
                passenger_count = self._parse_count(count_text)
                if passenger_count is None:
                    continue
                year_ago_count = None
                if len(cells) >= 3:
                    year_ago_text = cells[2].get_text(strip=True)
                    year_ago_count = self._parse_count(year_ago_text)
                data_points.append(TSADataPoint(
                    date=parsed_date,
                    passenger_count=passenger_count,
                    year_ago_count=year_ago_count,
                ))
            except Exception as e:
                logger.debug(f"Failed to parse row: {e}")
                continue
        data_points.sort(key=lambda x: x.date, reverse=True)
        return data_points

    def _parse_date(self, date_text: str) -> Optional[date]:
        formats = ["%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"]
        for fmt in formats:
            try:
                return datetime.strptime(date_text, fmt).date()
            except ValueError:
                continue
        logger.debug(f"Could not parse date: {date_text}")
        return None

    def _parse_count(self, count_text: str) -> Optional[int]:
        cleaned = re.sub(r"[^\d]", "", count_text)
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            return None

    async def get_latest_data(self) -> Optional[TSADataPoint]:
        try:
            html = await self.fetch_page()
            data_points = self.parse_html(html)
            if data_points:
                return data_points[0]
            return None
        except Exception as e:
            logger.error(f"Failed to get latest TSA data: {e}")
            return None

    async def check_for_new_data(self) -> Optional[TSADataPoint]:
        """Check if TSA has published new data.

        Uses conditional GET (If-Modified-Since) for lightweight polling.
        Only downloads and parses the full page when the server indicates
        content has changed.
        """
        try:
            html = await self.fetch_if_changed()
        except Exception as e:
            logger.error(f"Failed to check TSA data: {e}")
            return None

        if html is None:
            # 304 - content hasn't changed, no new data
            return None

        # Content changed - parse and check for new date
        data_points = self.parse_html(html)
        if not data_points:
            return None

        latest = data_points[0]

        if self._last_known_date is None:
            logger.info(f"Initial data point: {latest.date} - {latest.formatted_count}")
            self._last_known_date = latest.date
            return None

        if latest.date > self._last_known_date:
            logger.info(f"NEW DATA DETECTED: {latest.date} - {latest.formatted_count}")
            self._last_known_date = latest.date
            return latest

        logger.debug(f"Content changed but same date: {latest.date}")
        return None

    async def get_all_data(self) -> list[TSADataPoint]:
        try:
            html = await self.fetch_page()
            return self.parse_html(html)
        except Exception as e:
            logger.error(f"Failed to get TSA data: {e}")
            return []


async def test_scraper():
    """Test the TSA scraper."""
    logging.basicConfig(level=logging.INFO)
    print("Testing TSA Scraper...")
    print("=" * 50)
    async with TSAScraper() as scraper:
        print("")
        print("Fetching all data...")
        data_points = await scraper.get_all_data()
        if not data_points:
            print("ERROR: No data points found!")
            return
        print(f"Found {len(data_points)} data points")
        print("")
        print("Most recent 5 entries:")
        print("-" * 50)
        for dp in data_points[:5]:
            year_ago = f" (YoY: {dp.year_ago_count:,})" if dp.year_ago_count else ""
            bracket = dp.get_bracket()
            print(f"{dp.date}: {dp.formatted_count} passengers - Bracket: {bracket}{year_ago}")

        # Test conditional GET
        print("")
        print("Testing conditional GET (should get 304 on second fetch)...")
        html1 = await scraper.fetch_page()
        print(f"  First fetch: {len(html1)} bytes")
        html2 = await scraper.fetch_if_changed()
        if html2 is None:
            print(f"  Second fetch: 304 Not Modified (0 bytes transferred)")
        else:
            print(f"  Second fetch: {len(html2)} bytes (content changed)")
        print(f"  Stats: {scraper.conditional_stats}")

        print("")
        print("=" * 50)
        print("Scraper test complete!")


if __name__ == "__main__":
    asyncio.run(test_scraper())

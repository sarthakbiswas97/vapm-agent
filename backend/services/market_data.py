"""Market data service - fetches real-time and historical SOL/USDC data."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from config import get_settings
from db import Candle, get_session
from events.publisher import event_publisher

logger = logging.getLogger(__name__)
settings = get_settings()

# Birdeye SOL/USDC pair address on Solana mainnet
_SOLUSDC_PAIR = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"


@dataclass
class Tick:
    """Real-time price tick."""

    symbol: str
    price: float
    quantity: float
    timestamp: int  # Unix ms
    is_buyer_maker: bool


@dataclass
class CandleData:
    """OHLCV candle data."""

    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    num_trades: int
    is_closed: bool


class MarketDataService:
    """Handles all market data operations for SOL/USDC."""

    BIRDEYE_BASE_URL = "https://public-api.birdeye.so"
    JUPITER_PRICE_URL = "https://api.jup.ag/price/v2"

    def __init__(self, symbol: str = "SOLUSDC"):
        self.symbol = symbol
        self._running = False
        self._http_session: aiohttp.ClientSession | None = None

        # Latest data cache
        self.latest_price: float = 0.0
        self.latest_candle: CandleData | None = None

        # In-memory candle accumulator for building 1m candles from ticks
        self._current_candle_start: int = 0
        self._current_open: float = 0.0
        self._current_high: float = 0.0
        self._current_low: float = 0.0
        self._current_close: float = 0.0
        self._current_volume: float = 0.0
        self._current_trades: int = 0

    # ─────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the market data service."""
        logger.info("Starting MarketDataService for %s", self.symbol)

        await event_publisher.connect()

        self._http_session = aiohttp.ClientSession()

        await self._fetch_historical_candles(days=7)

        self._running = True
        asyncio.create_task(self._price_poll_loop())

        logger.info("MarketDataService started")

    async def stop(self) -> None:
        """Stop the market data service."""
        logger.info("Stopping MarketDataService...")
        self._running = False

        if self._http_session:
            await self._http_session.close()
            self._http_session = None

        await event_publisher.disconnect()
        logger.info("MarketDataService stopped")

    async def get_recent_candles(self, limit: int = 100) -> list[CandleData]:
        """Get recent candles from database."""
        async with get_session() as session:
            result = await session.execute(
                select(Candle)
                .where(Candle.symbol == self.symbol)
                .where(Candle.interval == "1m")
                .where(Candle.is_closed == True)  # noqa: E712
                .order_by(Candle.open_time.desc())
                .limit(limit)
            )
            rows = result.scalars().all()

            return [
                CandleData(
                    symbol=row.symbol,
                    interval=row.interval,
                    open_time=row.open_time,
                    close_time=row.close_time,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    quote_volume=row.quote_volume,
                    num_trades=row.num_trades,
                    is_closed=row.is_closed,
                )
                for row in reversed(rows)
            ]

    # ─────────────────────────────────────────────────────────────
    # HISTORICAL DATA (Birdeye API)
    # ─────────────────────────────────────────────────────────────

    async def _fetch_historical_candles(self, days: int = 7) -> None:
        """Fetch historical 1m candles from Birdeye OHLCV API."""
        logger.info("Fetching %d days of historical candles from Birdeye...", days)

        if not self._http_session:
            return

        now = int(datetime.now(tz=timezone.utc).timestamp())
        start = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())

        headers = self._birdeye_headers()
        all_candles: list[CandleData] = []
        current_start = start

        while current_start < now:
            url = f"{self.BIRDEYE_BASE_URL}/defi/ohlcv"
            params = {
                "address": _SOLUSDC_PAIR,
                "type": "1m",
                "time_from": current_start,
                "time_to": min(current_start + 86400, now),  # 1 day chunks
            }

            try:
                async with self._http_session.get(
                    url, params=params, headers=headers
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Birdeye API error %d, falling back to Jupiter",
                            resp.status,
                        )
                        break

                    body = await resp.json()
                    items = body.get("data", {}).get("items", [])

                    if not items:
                        current_start += 86400
                        continue

                    for item in items:
                        open_time_ms = int(item["unixTime"]) * 1000
                        candle = CandleData(
                            symbol=self.symbol,
                            interval="1m",
                            open_time=open_time_ms,
                            close_time=open_time_ms + 59999,
                            open=float(item["o"]),
                            high=float(item["h"]),
                            low=float(item["l"]),
                            close=float(item["c"]),
                            volume=float(item.get("v", 0)),
                            quote_volume=float(item.get("v", 0)),
                            num_trades=0,
                            is_closed=True,
                        )
                        all_candles.append(candle)

                    last_ts = int(items[-1]["unixTime"])
                    current_start = last_ts + 60

            except Exception as e:
                logger.error("Error fetching Birdeye candles: %s", e)
                break

            await asyncio.sleep(0.2)

        await self._store_candles(all_candles)
        logger.info("Stored %d historical candles", len(all_candles))

    async def _store_candles(self, candles: list[CandleData]) -> None:
        """Store candles in PostgreSQL with upsert."""
        if not candles:
            return

        async with get_session() as session:
            for candle in candles:
                stmt = (
                    insert(Candle)
                    .values(
                        symbol=candle.symbol,
                        interval=candle.interval,
                        open_time=candle.open_time,
                        close_time=candle.close_time,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        quote_volume=candle.quote_volume,
                        num_trades=candle.num_trades,
                        is_closed=candle.is_closed,
                    )
                    .on_conflict_do_update(
                        index_elements=["symbol", "interval", "open_time"],
                        set_={
                            "high": candle.high,
                            "low": candle.low,
                            "close": candle.close,
                            "volume": candle.volume,
                            "quote_volume": candle.quote_volume,
                            "num_trades": candle.num_trades,
                            "is_closed": candle.is_closed,
                        },
                    )
                )
                await session.execute(stmt)

    # ─────────────────────────────────────────────────────────────
    # REAL-TIME PRICE POLLING (Jupiter Price API)
    # ─────────────────────────────────────────────────────────────

    async def _price_poll_loop(self) -> None:
        """Poll Jupiter Price API every 5 seconds for real-time SOL/USDC."""
        poll_interval = 5  # seconds
        reconnect_delay = 1

        while self._running:
            try:
                await self._poll_price()
                reconnect_delay = 1
                await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.error("Price poll error: %s", e)
                if self._running:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)

    async def _poll_price(self) -> None:
        """Fetch current SOL price from Jupiter and emit tick + candle events."""
        if not self._http_session:
            return

        params = {"ids": settings.sol_mint}
        async with self._http_session.get(
            self.JUPITER_PRICE_URL, params=params
        ) as resp:
            if resp.status != 200:
                logger.warning("Jupiter price API returned %d", resp.status)
                return

            body = await resp.json()
            sol_data = body.get("data", {}).get(settings.sol_mint)
            if not sol_data:
                return

            price = float(sol_data["price"])
            now_ms = int(time.time() * 1000)

        self.latest_price = price

        # Emit tick
        tick = Tick(
            symbol=self.symbol,
            price=price,
            quantity=0.0,
            timestamp=now_ms,
            is_buyer_maker=False,
        )
        await self._emit_tick(tick)

        # Accumulate into 1-minute candle
        await self._accumulate_candle(price, now_ms)

    async def _emit_tick(self, tick: Tick) -> None:
        """Publish tick to Redis streams and events."""
        await event_publisher.add_to_stream(
            "stream:ticks",
            {
                "symbol": tick.symbol,
                "price": tick.price,
                "quantity": tick.quantity,
                "timestamp": tick.timestamp,
                "is_buyer_maker": tick.is_buyer_maker,
            },
            maxlen=1000,
        )

        await event_publisher.set_json(
            "market:latest_price",
            {
                "symbol": tick.symbol,
                "price": tick.price,
                "timestamp": tick.timestamp,
            },
        )

        await event_publisher.publish(
            "event:tick",
            {
                "symbol": tick.symbol,
                "price": tick.price,
                "timestamp": tick.timestamp,
            },
        )

    async def _accumulate_candle(self, price: float, now_ms: int) -> None:
        """Build 1-minute candles from polled prices."""
        minute_start = (now_ms // 60_000) * 60_000

        if minute_start != self._current_candle_start:
            # New minute boundary -- close previous candle if exists
            if self._current_candle_start > 0:
                closed_candle = CandleData(
                    symbol=self.symbol,
                    interval="1m",
                    open_time=self._current_candle_start,
                    close_time=self._current_candle_start + 59999,
                    open=self._current_open,
                    high=self._current_high,
                    low=self._current_low,
                    close=self._current_close,
                    volume=self._current_volume,
                    quote_volume=self._current_volume,
                    num_trades=self._current_trades,
                    is_closed=True,
                )
                self.latest_candle = closed_candle
                await self._store_candles([closed_candle])

                await event_publisher.publish(
                    "event:candle_closed",
                    {
                        "symbol": closed_candle.symbol,
                        "interval": closed_candle.interval,
                        "open_time": closed_candle.open_time,
                        "close": closed_candle.close,
                        "volume": closed_candle.volume,
                    },
                )

            # Start new candle
            self._current_candle_start = minute_start
            self._current_open = price
            self._current_high = price
            self._current_low = price
            self._current_close = price
            self._current_volume = 0.0
            self._current_trades = 0
        else:
            # Update current candle
            self._current_high = max(self._current_high, price)
            self._current_low = min(self._current_low, price)
            self._current_close = price
            self._current_trades += 1

        # Publish current (open) candle state to Redis
        await event_publisher.set_json(
            "market:current_candle",
            {
                "symbol": self.symbol,
                "interval": "1m",
                "open": self._current_open,
                "high": self._current_high,
                "low": self._current_low,
                "close": self._current_close,
                "volume": self._current_volume,
                "is_closed": False,
            },
        )

    def _birdeye_headers(self) -> dict[str, str]:
        """Build Birdeye API headers."""
        headers = {"accept": "application/json"}
        if settings.birdeye_api_key:
            headers["X-API-KEY"] = settings.birdeye_api_key
        return headers


# Global singleton
market_data_service = MarketDataService()

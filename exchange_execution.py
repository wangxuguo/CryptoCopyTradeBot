# exchange_execution.py

from enum import Enum
import os
import asyncio
import json 
import math
import hmac
import hashlib
import base64
from urllib.parse import urlencode
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, List, Optional, Tuple, Union
from dataclasses import dataclass, field, asdict

import ccxt
import aiohttp
from urllib.parse import urljoin

from models import (
    TradingSignal,
    OrderResult,
    EntryZone,
    TakeProfitLevel  
)

# Constants
class OrderSide:
    BUY = 'buy'
    SELL = 'sell'

class OrderType:
    MARKET = 'MARKET'
    LIMIT = 'LIMIT'
    STOP = 'STOP'
    STOP_MARKET = 'STOP_MARKET'
    TAKE_PROFIT = 'TAKE_PROFIT'
    TAKE_PROFIT_MARKET = 'TAKE_PROFIT_MARKET'

class OrderStatus:
    PENDING = 'PENDING'
    OPEN = 'OPEN'
    CLOSED = 'CLOSED'
    CANCELED = 'CANCELED'
    EXPIRED = 'EXPIRED'
    REJECTED = 'REJECTED'


class PositionSide(str, Enum):
    """Position side enum"""
    LONG = 'long'
    SHORT = 'short'

class MarginType(str, Enum):
    """Margin type enum"""
    CROSS = 'cross'
    ISOLATED = 'isolated'

@dataclass 
class ExchangeCredentials:
    """Exchange credentials configuration"""
    api_key: str
    api_secret: str
    passphrase: Optional[str] = None
    test_api_key: Optional[str] = None
    test_api_secret: Optional[str] = None
    test_passphrase: Optional[str] = None
    testnet: bool = False

# 或者如果你希望保持完全一致的话,可以这样定义:
@dataclass 
class OrderParams:
    """Order parameters (Legacy class for backwards compatibility)"""
    symbol: str
    side: str 
    order_type: str
    amount: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    leverage: Optional[int] = None
    margin_mode: str = MarginType.CROSS
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> bool:
        """Validate order parameters"""
        try:
            if not all([self.symbol, self.side, self.order_type, self.amount > 0]):
                return False
            
            if self.order_type == OrderType.LIMIT and not self.price:
                return False
                
            if self.order_type in [OrderType.STOP, OrderType.TAKE_PROFIT] and not self.stop_price:
                return False
                
            return True
            
        except Exception:
            return False


@dataclass 
class OrderInfo:
    """Order information"""
    id: str
    symbol: str
    side: str
    type: str
    price: Optional[float]
    amount: float
    filled: float = 0
    remaining: float = 0
    status: str = OrderStatus.PENDING
    fee: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    @staticmethod
    def from_exchange_order(order: Dict[str, Any]) -> Optional['OrderInfo']:
        """Create OrderInfo from exchange order data"""
        try:
            return OrderInfo(
                id=order['id'],
                symbol=order['symbol'],
                side=order['side'],
                type=order['type'],
                price=float(order['price']) if order.get('price') else None,
                amount=float(order['amount']),
                filled=float(order.get('filled', 0)),
                remaining=float(order.get('remaining', order['amount'])),
                status=order['status'],
                fee=order.get('fee', {}),
                timestamp=datetime.fromtimestamp(order['timestamp']/1000)
            )
        except Exception as e:
            logging.error(f"Error creating OrderInfo: {e}")
            return None

# 首先修复数据模型
@dataclass
class AccountBalance:
    """Account balance information"""
    total_equity: float = 0.0  # 总权益
    used_margin: float = 0.0   # 已用保证金
    free_margin: float = 0.0   # 可用保证金
    margin_ratio: float = 0.0  # 保证金率
    unrealized_pnl: float = 0.0  # 未实现盈亏
    realized_pnl: float = 0.0    # 已实现盈亏
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def total(self) -> float:
        return self.total_equity

    @property
    def used(self) -> float:
        return self.used_margin

    @property
    def free(self) -> float:
        return self.free_margin

    @staticmethod
    def from_exchange_balance(balance: Dict[str, Any]) -> 'AccountBalance':
        """Create AccountBalance from exchange balance data"""
        try:
            total = float(balance.get('total', {}).get('USDT', 0) or 0)
            used = float(balance.get('used', {}).get('USDT', 0) or 0)
            free = float(balance.get('free', {}).get('USDT', 0) or 0)
            
            return AccountBalance(
                total_equity=total,
                used_margin=used,
                free_margin=free,
                margin_ratio=(used / total * 100) if total > 0 else 0,
                unrealized_pnl=float(balance.get('unrealizedPnl', 0) or 0),
                realized_pnl=float(balance.get('realizedPnl', 0) or 0)
            )
        except Exception as e:
            logging.error(f"Error creating AccountBalance: {e}")
            return AccountBalance()


@dataclass
class PositionInfo:
    """Position information"""
    # 基础信息
    symbol: str
    side: PositionSide
    size: float
    entry_price: float
    margin_mode: MarginType
    leverage: int
    
    # 价格相关
    liquidation_price: Optional[float] = None
    mark_price: Optional[float] = 0
    break_even_price: Optional[float] = None
    
    # 保证金相关
    initial_margin: float = 0
    maintenance_margin: float = 0
    position_initial_margin: float = 0  # 持仓保证金
    open_order_initial_margin: float = 0  # 委托单保证金
    isolated_margin: float = 0  # 逐仓保证金
    
    # 盈亏相关
    unrealized_pnl: float = 0
    realized_pnl: float = 0
    pnl_percentage: float = 0
    
    # 其他
    notional: float = 0  # 名义价值
    collateral: float = 0  # 可用保证金
    timestamp: datetime = field(default_factory=datetime.now)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Support dict-like get method for compatibility"""
        return getattr(self, key, default)

    @staticmethod
    def from_exchange_position(pos: Dict[str, Any]) -> Optional['PositionInfo']:
        """Create PositionInfo from exchange position data"""
        try:
            logging.info(f"ExchangeInfo -- {pos}")
            
            # 处理空仓位情况
            position_amt = float(pos.get('contracts', 0) or pos.get('positionAmt', 0) or 0)
            if position_amt == 0:
                return None
            
            
            # 判断多空方向
            side = PositionSide.LONG if (
                pos.get('side') == 'long' 
            ) else PositionSide.SHORT
            
            # 判断杠杆模式
            margin_mode = MarginType.ISOLATED if pos.get('marginMode') == 'isolated' else MarginType.CROSS
            
            return PositionInfo(
                # 基础信息
                symbol=pos['symbol'],
                side=side,
                size=abs(position_amt),
                entry_price=float(pos.get('entryPrice', 0) or 0),
                margin_mode=margin_mode,
                leverage=int(float(pos.get('leverage', 1) or 1)),
                
                # 价格相关
                liquidation_price=float(pos.get('liquidationPrice', 0) or 0),
                mark_price=float(pos.get('markPrice', 0) or 0),
                break_even_price=float(pos.get('breakEvenPrice', 0) or 0),
                
                # 保证金相关
                initial_margin=float(pos.get('initialMargin', 0) or 0),
                maintenance_margin=float(pos.get('maintenanceMargin', 0) or 0),
                position_initial_margin=float(pos.get('positionInitialMargin', 0) or 0),
                open_order_initial_margin=float(pos.get('openOrderInitialMargin', 0) or 0),
                isolated_margin=float(pos.get('isolatedMargin', 0) or 0),
                
                # 盈亏相关
                unrealized_pnl=float(pos.get('unrealizedPnl', 0) or pos.get('unRealizedProfit', 0) or 0),
                realized_pnl=float(pos.get('realizedPnl', 0) or 0),
                pnl_percentage=float(pos.get('percentage', 0) or 0),
                
                # 其他
                notional=float(pos.get('notional', 0) or 0),
                collateral=float(pos.get('collateral', 0) or 0)
            )
        except Exception as e:
            logging.error(f"Error creating PositionInfo: {e}")
            return None

    def is_long(self) -> bool:
        """Check if position is long"""
        return self.side == PositionSide.LONG
    
    def is_short(self) -> bool:
        """Check if position is short"""
        return self.side == PositionSide.SHORT
    
    def is_isolated(self) -> bool:
        """Check if position is isolated margin"""
        return self.margin_mode == MarginType.ISOLATED
    
    def is_cross(self) -> bool:
        """Check if position is cross margin"""
        return self.margin_mode == MarginType.CROSS

@dataclass
class MarketInfo:
    """Market information"""
    symbol: str
    base: str
    quote: str
    price_precision: int
    amount_precision: int
    min_amount: float
    min_cost: float 
    market_type: str
    contract_size: float = 1.0
    last_price: Optional[float] = None
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)

    @staticmethod
    def from_exchange_market(market: Dict[str, Any], ticker: Optional[Dict[str, Any]] = None) -> Optional['MarketInfo']:
        """Create MarketInfo from exchange market data"""
        try:
            def safe_float(value: Any, default: float = 0.0) -> float:
                if value is None:
                    return default
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return default

            precision = market.get('precision', {})
            limits = market.get('limits', {})
            info = market.get('info', {})
            
            return MarketInfo(
                symbol=market['symbol'],
                base=market.get('base', ''),
                quote=market.get('quote', ''),
                price_precision=int(precision.get('price', 8)),
                amount_precision=int(precision.get('amount', 8)),
                min_amount=(
                    safe_float(limits.get('amount', {}).get('min'))
                    if limits.get('amount', {}) else safe_float(info.get('minSz'))
                ),
                min_cost=safe_float(limits.get('cost', {}).get('min')),
                market_type=market.get('type', 'swap' if info.get('instType') == 'SWAP' else 'spot'),
                contract_size=(
                    safe_float(market.get('contractSize'), 0.0) or
                    safe_float(info.get('ctVal'), 1.0)
                ),
                last_price=safe_float(ticker.get('last')) if ticker else None,
                mark_price=safe_float(ticker.get('mark')) if ticker else None,
                index_price=safe_float(ticker.get('index')) if ticker else None
            )
        except Exception as e:
            logging.error(f"Error creating MarketInfo: {e}")
            return None


class ExchangeException(Exception):
    """Base exchange exception"""
    pass

class OrderException(ExchangeException):
    """Order related exception"""
    pass

class PositionException(ExchangeException):
    """Position related exception"""
    pass 

class MarketException(ExchangeException):
    """Market data related exception"""
    pass

class NetworkException(ExchangeException):
    """Network related exception"""
    pass

class ExchangeClient(ABC):
    """Base exchange client implementation"""

    def __init__(self, credentials: ExchangeCredentials):
        self.credentials = credentials
        self._exchange = None
        self._last_request_time = 0
        self.min_request_interval = 0.1
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Cache
        self._market_cache: Dict[str, MarketInfo] = {}
        self._market_cache_time: Dict[str, float] = {}
        self._balance_cache: Optional[AccountBalance] = None 
        self._balance_cache_time: float = 0
        self._position_cache: Dict[str, PositionInfo] = {}
        self._position_cache_time: Dict[str, float] = {}
        
        self.CACHE_DURATION = 5  # seconds

    async def initialize(self) -> bool:
        """Initialize exchange client"""
        try:
            # Setup exchange
            if not await self._setup_exchange():
                return False
                
            # Load markets
            await self._load_markets()
            
            # Initialize session
            if not self._session:
                self._session = aiohttp.ClientSession()
                
            return True
            
        except Exception as e:
            logging.error(f"Error initializing exchange client: {e}")
            return False

    async def cleanup(self):
        """Cleanup resources"""
        try:
            if self._session:
                await self._session.close()
            self._session = None
            self._exchange = None
        except Exception as e:
            logging.error(f"Error cleaning up exchange client: {e}")

    def _rate_limit(self):
        """Apply rate limiting"""
        current_time = time.time()
        time_since_last = current_time - self._last_request_time
        if time_since_last < self.min_request_interval:
            time.sleep(self.min_request_interval - time_since_last)
        self._last_request_time = time.time()

    def _normalize_symbol(self, symbol: str) -> str:
        s = (symbol or "").upper().strip()
        if not s:
            return s
        if "/" in s and ":USDT" in s:
            return s
        if "/" in s:
            base, quote = s.split("/")[0], s.split("/")[1]
        elif "-" in s:
            parts = s.split("-")
            base = parts[0]
            quote = parts[1] if len(parts) > 1 else "USDT"
        else:
            base = s.replace("USDT", "")
            quote = "USDT"
        if getattr(self, "exchange_name", "") == "OKX":
            return f"{base}/{quote}:USDT"
        return f"{base}/{quote}"

    async def _okx_request(self, path: str, method: str, payload: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ts = datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'
        m = method.upper()
        q = urlencode(query or {})
        qs = f"?{q}" if q else ""
        body = "" if m == 'GET' else json.dumps(payload or {}, separators=(',', ':'))
        prehash = f"{ts}{m}{path}{qs}{body}"
        sign = hmac.new(self.credentials.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        sign_b64 = base64.b64encode(sign).decode()
        headers = {
            'OK-ACCESS-KEY': self.credentials.api_key,
            'OK-ACCESS-SIGN': sign_b64,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': self.credentials.passphrase or '',
            'Content-Type': 'application/json'
        }
        if getattr(self.credentials, 'testnet', False):
            headers['x-simulated-trading'] = '1'
        url = f"https://www.okx.com{path}{qs}"
        if m == 'GET':
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
                return data
        else:
            async with self._session.post(url, headers=headers, data=body) as resp:
                data = await resp.json()
                return data

    async def _okx_create_order(self, symbol: str, type_arg: str, side_arg: str, amount_arg: float, price_arg: Optional[float], params: Dict[str, Any]) -> Dict[str, Any]:
        market = await asyncio.to_thread(self._exchange.market, symbol)
        inst_id = market.get('id')
        body: Dict[str, Any] = {
            'instId': inst_id,
            'tdMode': str(params.get('tdMode') or 'cross').lower(),
            'side': side_arg.lower(),
            'ordType': type_arg.lower(),
            'sz': str(int(amount_arg)) if isinstance(amount_arg, (int, float)) else str(amount_arg)
        }
        if price_arg is not None and type_arg == 'limit':
            body['px'] = str(price_arg)
        if 'posSide' in params:
            body['posSide'] = str(params['posSide']).lower()
        if 'reduceOnly' in params:
            body['reduceOnly'] = params['reduceOnly']
        if 'lever' in params:
            body['lever'] = str(params['lever'])
        if 'clOrdId' in params:
            body['clOrdId'] = params['clOrdId']
        # Inline TP/SL attachment when provided
        tp_px = params.get('tpTriggerPx') or params.get('takeProfitPrice')
        sl_px = params.get('slTriggerPx') or params.get('stopLossPrice')
        if tp_px is not None:
            body['tpTriggerPx'] = str(tp_px)
            body['tpOrdPx'] = '-1'
            body['tpTriggerPxType'] = 'last'
        if sl_px is not None:
            body['slTriggerPx'] = str(sl_px)
            body['slOrdPx'] = '-1'
            body['slTriggerPxType'] = 'last'
        return await self._okx_request('/api/v5/trade/order', 'POST', body)

    async def _okx_get_price_limit(self, symbol: str) -> Optional[Dict[str, float]]:
        try:
            market = await asyncio.to_thread(self._exchange.market, symbol)
            inst_id = market.get('id')
            raw = await self._okx_request('/api/v5/public/price-limit', 'GET', None, {'instId': inst_id})
            if raw and str(raw.get('code')) == '0':
                d = (raw.get('data') or [{}])[0]
                max_buy = d.get('buyLmtPx') or d.get('buyPx') or d.get('buyLmt')
                min_sell = d.get('sellLmtPx') or d.get('sellPx') or d.get('sellLmt')
                return {
                    'max_buy': float(max_buy) if max_buy is not None else None,
                    'min_sell': float(min_sell) if min_sell is not None else None,
                }
            return None
        except Exception as e:
            logging.warning(f"Failed to fetch OKX price limit: {e}")
            return None

    async def _okx_attach_tp_sl(self, symbol: str, side_close: str, amount_contracts: int, td_mode: str,
                                pos_side: Optional[str], tp_price: Optional[float], sl_price: Optional[float]) -> Dict[str, Any]:
        market = await asyncio.to_thread(self._exchange.market, symbol)
        inst_id = market.get('id')
        body: Dict[str, Any] = {
            'instId': inst_id,
            'side': side_close.lower(),
            'tdMode': str(td_mode).lower(),
            'sz': str(int(amount_contracts)),
        }
        if pos_side:
            body['posSide'] = pos_side.lower()
        # Prefer OCO when both provided, otherwise conditional
        if tp_price is not None and sl_price is not None:
            body['ordType'] = 'oco'
            body['tpTriggerPx'] = str(tp_price)
            body['tpOrdPx'] = '-1'
            body['tpTriggerPxType'] = 'last'
            body['slTriggerPx'] = str(sl_price)
            body['slOrdPx'] = '-1'
            body['slTriggerPxType'] = 'last'
        elif tp_price is not None:
            body['ordType'] = 'conditional'
            body['tpTriggerPx'] = str(tp_price)
            body['tpOrdPx'] = '-1'
            body['tpTriggerPxType'] = 'last'
        elif sl_price is not None:
            body['ordType'] = 'conditional'
            body['slTriggerPx'] = str(sl_price)
            body['slOrdPx'] = '-1'
            body['slTriggerPxType'] = 'last'
        else:
            return {'code': '0', 'data': []}
        return await self._okx_request('/api/v5/trade/order-algo', 'POST', body)

    @abstractmethod
    async def _setup_exchange(self) -> bool:
        """Setup exchange connection"""
        pass

    async def _load_markets(self) -> None:
        """Load market data"""
        try:
            if not self._exchange:
                raise ValueError("Exchange not initialized")
                
            self._rate_limit()
            markets = await asyncio.to_thread(self._exchange.load_markets)
            
            # Cache market info
            for symbol, market in markets.items():
                market_info = MarketInfo.from_exchange_market(market)
                if market_info:
                    self._market_cache[symbol] = market_info
                    self._market_cache_time[symbol] = time.time()
                    
            logging.info(f"Loaded {len(self._market_cache)} markets")
            
        except Exception as e:
            logging.error(f"Error loading markets: {e}")
            raise



    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        """Safely convert value to float"""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        """Fetch positions"""
        try:
            self._rate_limit()
            norm = self._normalize_symbol(symbol) if symbol else None
            positions = await asyncio.to_thread(
                self._exchange.fetchPositions,
                [norm] if norm else None
            )
            
            result = []
            for pos in positions:
                position_info = PositionInfo.from_exchange_position(pos)
                if position_info:
                    result.append(position_info)
            return result
            
        except Exception as e:
            logging.error(f"Error fetching positions: {e}")
            return []

    async def fetch_balance(self) -> AccountBalance:
        """Fetch balance"""
        try:
            self._rate_limit()
            balance = await asyncio.to_thread(self._exchange.fetchBalance)
            return AccountBalance.from_exchange_balance(balance)
        except Exception as e:
            logging.error(f"Error fetching balance: {e}")
            return AccountBalance()

    async def get_market_info(self, symbol: str) -> Optional[MarketInfo]:
        """Get market information"""
        try:
            now = time.time()
            norm = self._normalize_symbol(symbol)
            if norm in self._market_cache:
                if now - self._market_cache_time.get(norm, 0) < self.CACHE_DURATION:
                    return self._market_cache[norm]
            
            self._rate_limit()
            market = await asyncio.to_thread(self._exchange.market, norm)
            ticker = await asyncio.to_thread(self._exchange.fetchTicker, norm)
            
            market_info = MarketInfo.from_exchange_market(market, ticker)
            if market_info:
                self._market_cache[norm] = market_info
                self._market_cache_time[norm] = now
            return market_info
            
        except Exception as e:
            logging.error(f"Error getting market info: {e}")
            return None

    async def get_balance(self) -> AccountBalance:
        """Get account balance"""
        try:
            # Check cache
            now = time.time()
            if self._balance_cache and now - self._balance_cache_time < self.CACHE_DURATION:
                return self._balance_cache
                
            # Fetch from exchange
            self._rate_limit()
            balance = await asyncio.to_thread(self._exchange.fetchBalance)
            
            balance_info = AccountBalance.from_exchange_balance(balance)
            self._balance_cache = balance_info
            self._balance_cache_time = now
            
            return balance_info
            
        except Exception as e:
            logging.error(f"Error getting balance: {e}")
            return AccountBalance()

    async def get_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        """Get positions"""
        try:
            # Check cache
            now = time.time()
            cache_key = symbol or 'all'
            if cache_key in self._position_cache:
                if now - self._position_cache_time.get(cache_key, 0) < self.CACHE_DURATION:
                    return [self._position_cache[cache_key]]
                    
            # Fetch from exchange
            self._rate_limit()
            positions = await asyncio.to_thread(
                self._exchange.fetchPositions,
                [symbol] if symbol else None
            )
            
            result = []
            for pos in positions:
                if float(pos.get('contracts', 0)) != 0:  # Only include non-zero positions
                    position_info = PositionInfo.from_exchange_position(pos)
                    if position_info:
                        result.append(position_info)
                        self._position_cache[position_info.symbol] = position_info
                        self._position_cache_time[position_info.symbol] = now
                        
            if not symbol:  # Cache all positions
                self._position_cache['all'] = result
                self._position_cache_time['all'] = now
                
            return result
            
        except Exception as e:
            logging.error(f"Error getting positions: {e}")
            return []


    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel order"""
        try:
            self._rate_limit()
            result = await asyncio.to_thread(
                self._exchange.cancelOrder,
                order_id,
                symbol
            )
            return bool(result)
        except Exception as e:
            logging.error(f"Error canceling order: {e}")
            return False

    async def get_order(self, order_id: str, symbol: str) -> Optional[OrderInfo]:
        """Get order information"""
        try:
            self._rate_limit()
            order = await asyncio.to_thread(
                self._exchange.fetchOrder,
                order_id,
                symbol
            )
            return OrderInfo.from_exchange_order(order) if order else None
        except Exception as e:
            logging.error(f"Error getting order: {e}")
            return None

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderInfo]:
        try:
            self._rate_limit()
            norm = self._normalize_symbol(symbol) if symbol else None
            orders = await asyncio.to_thread(
                self._exchange.fetchOpenOrders,
                norm
            )
            result: List[OrderInfo] = []
            for o in orders or []:
                info = OrderInfo.from_exchange_order(o)
                if info:
                    result.append(info)
            return result
        except Exception as e:
            logging.error(f"Error getting open orders: {e}")
            return []

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get funding rate"""
        try:
            self._rate_limit()
            funding = await asyncio.to_thread(
                self._exchange.fetchFundingRate,
                symbol
            )
            return float(funding['fundingRate']) if funding else None
        except Exception as e:
            logging.error(f"Error getting funding rate: {e}")
            return None

    async def get_mark_price_history(self, symbol: str, timeframe: str = '1m',
                                   limit: int = 100) -> List[Dict[str, Any]]:
        """Get mark price history"""
        try:
            self._rate_limit()
            ohlcv = await asyncio.to_thread(
                self._exchange.fetchOHLCV,
                symbol,
                timeframe,
                limit=limit,
                params={'price': 'mark'}
            )
            
            return [
                {
                    'timestamp': datetime.fromtimestamp(candle[0]/1000),
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': float(candle[4]),
                    'volume': float(candle[5])
                }
                for candle in ohlcv
            ]
        except Exception as e:
            logging.error(f"Error getting mark price history: {e}")
            return []


    async def get_market_leverage_info(self, symbol: str) -> Dict[str, Any]:
        """Get market leverage settings"""
        try:
            # Get market leverage info from exchange
            leverage_info = await asyncio.to_thread(
                self._exchange.fetchMarketLeverageTiers,  # 注意这里是fetchMarketLeverageTiers
                symbol  # 不需要放在列表里
            )
            
            if leverage_info and isinstance(leverage_info, list):
                # 通常第一个tier是最大杠杆
                max_leverage = int(leverage_info[0].get('maxLeverage', 1))
                return {
                    'max_leverage': max_leverage,
                    'tiers': leverage_info
                }
            return {'max_leverage': 1, 'tiers': []}
            
        except Exception as e:
            logging.error(f"Error getting leverage info: {e}")
            return {'max_leverage': 1, 'tiers': []}

    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str) -> bool:
        """Set leverage with validation"""
        try:
            leverage_info = await self.get_market_leverage_info(self._normalize_symbol(symbol))
            max_leverage = leverage_info['max_leverage']
            
            actual_leverage = min(leverage, max_leverage)
            
            norm = self._normalize_symbol(symbol)
            if getattr(self, 'exchange_name', '') == 'OKX':
                return actual_leverage
            else:
                await asyncio.to_thread(
                    self._exchange.setMarginMode,
                    margin_mode,
                    norm
                )
                await asyncio.to_thread(
                    self._exchange.setLeverage,
                    actual_leverage,
                    norm
                )
            
            logging.info(f"Set {margin_mode} leverage for {symbol}: requested={leverage}, actual={actual_leverage}")
            return actual_leverage
            
        except Exception as e:
            logging.error(f"Error setting leverage: {e}")
            raise  # 这种关键操作最好抛出异常而不是返回False

    async def convert_amount_to_contracts(
        self, 
        symbol: str, 
        usdt_amount: float, 
        price: float,
        leverage: int
    ) -> Tuple[float, Dict[str, Any]]:
        """Convert USDT amount to contracts quantity with leverage"""
        try:
            market_info = await self.get_market_info(symbol)
            if not market_info:
                raise ValueError(f"Cannot get market info for {symbol}")

            # Get max allowed leverage and adjust if needed
            leverage_info = await self.get_market_leverage_info(self._normalize_symbol(symbol))
            actual_leverage = min(leverage, leverage_info['max_leverage'])
            
            is_contract = (market_info.amount_precision == 0)
            if is_contract:
                ct = float(market_info.contract_size or 0.0)
                if ct <= 0:
                    try:
                        mkt = await asyncio.to_thread(self._exchange.market, self._normalize_symbol(symbol))
                        info = mkt.get('info', {})
                        ct = float(info.get('ctVal') or 1.0)
                    except Exception:
                        ct = 1.0
                contracts_raw = (usdt_amount * actual_leverage) / (price * ct)
                formatted_quantity = max(1, math.floor(contracts_raw))
                # if formatted_quantity < 1:
                #     raise OrderException("Amount too small to buy minimum 1 contract with given budget")
                # formatted_quantity = max(1, math.floor(contracts_raw))
                try:
                    min_qty = market_info.min_amount or 0
                    if min_qty == 0:
                        mkt = await asyncio.to_thread(self._exchange.market, self._normalize_symbol(symbol))
                        min_qty = float(mkt.get('info', {}).get('minSz') or 0)
                    if min_qty and formatted_quantity < min_qty:
                        formatted_quantity = int(max(1, math.floor(min_qty)))
                except Exception:
                    pass
                notional_calc = formatted_quantity * price * ct
                actual_value = notional_calc / actual_leverage
                raw_quantity = contracts_raw
                notional_value_calc = notional_calc
            else:
                notional_value = usdt_amount * actual_leverage
                quantity = notional_value / price
                formatted_quantity = self._format_amount(self._normalize_symbol(symbol), quantity)
                try:
                    min_qty = market_info.min_amount or 0
                    if min_qty and formatted_quantity < min_qty:
                        raise OrderException(f"Amount {formatted_quantity} is below exchange min amount {min_qty}")
                except Exception:
                    pass
                actual_value = (formatted_quantity * price) / actual_leverage
                raw_quantity = quantity
                notional_value_calc = formatted_quantity * price
            if actual_value > usdt_amount:
                logging.warning(f"Insufficient budget: initial margin {actual_value:.2f} exceeds {usdt_amount:.2f}; proceeding with minimum size")

            logging.info(f"""
    Amount Conversion Details:
    USDT Amount: {usdt_amount}
    Price: {price}
    Leverage: {actual_leverage}x (max: {leverage_info['max_leverage']}x)
    Raw Quantity: {raw_quantity}
    Formatted Quantity: {formatted_quantity}
    Actual Margin: {actual_value} USDT
    Notional Value: {notional_value_calc} USDT
    Min Amount: {market_info.min_amount if market_info else 'Unknown'}
    Amount Precision: {market_info.amount_precision if market_info else 'Unknown'}
    """)

            return formatted_quantity, {
                'raw_quantity': raw_quantity,
                'formatted_quantity': formatted_quantity,
                'initial_margin': actual_value,
                'notional_value': notional_value_calc,
                'price': price,
                'leverage': actual_leverage
            }

        except Exception as e:
            logging.error(f"Error converting amount to contracts: {e}")
            raise

    async def create_order(self, order: OrderParams) -> OrderResult:
        """Create order with proper price and leverage handling"""
        try:
            if order.order_type == OrderType.LIMIT and not order.price:
                logging.warning(f"Limit order missing price for {order.symbol}, switching to MARKET")
                order.order_type = OrderType.MARKET
            if not order.validate():
                logging.error(f"Order validation failed: symbol={order.symbol}, side={order.side}, type={order.order_type}, amount={order.amount}, price={order.price}, stop_price={order.stop_price}, reduce_only={order.reduce_only}")
                raise OrderException("Invalid order parameters")
                
            ccxt_symbol = self._normalize_symbol(order.symbol)
            market_info = await self.get_market_info(ccxt_symbol)
            if not market_info or not market_info.last_price:
                raise ValueError(f"Cannot get market info for {order.symbol}")

            # 确定使用价格
            use_price = order.price if (order.order_type == OrderType.LIMIT and order.price) else market_info.last_price
            
            # 设置杠杆和保证金模式
            leverage = order.leverage or 50  # 默认50倍杠杆
            actual_leverage = await self.set_leverage(ccxt_symbol, leverage, order.margin_mode)
            
            quantity, conversion_info = await self.convert_amount_to_contracts(
                ccxt_symbol,
                order.amount,  # USDT amount
                use_price,
                actual_leverage  # 使用实际设置的杠杆
            )

            # 创建订单参数（统一方法签名 + 额外参数）
            symbol_arg = ccxt_symbol
            type_arg = order.order_type.lower()
            side_arg = order.side.lower()
            amount_arg = quantity
            price_arg = None
            params_extras: Dict[str, Any] = {}

            if order.order_type == OrderType.LIMIT:
                if not order.price:
                    raise OrderException("Price is required for limit orders")
                price_arg = self._format_price(ccxt_symbol, order.price)
                if getattr(self, 'exchange_name', '') == 'OKX':
                    limits = await self._okx_get_price_limit(ccxt_symbol)
                    if limits and side_arg == 'buy' and limits.get('max_buy') is not None and price_arg > limits['max_buy']:
                        logging.warning(f"Limit price {price_arg} exceeds max buy {limits['max_buy']}, clamping")
                        price_arg = limits['max_buy']
                    if limits and side_arg == 'sell' and limits.get('min_sell') is not None and price_arg < limits['min_sell']:
                        logging.warning(f"Limit price {price_arg} below min sell {limits['min_sell']}, clamping")
                        price_arg = limits['min_sell']
            
            if order.stop_price:
                params_extras['stopPrice'] = self._format_price(ccxt_symbol, order.stop_price)
            if order.reduce_only:
                params_extras['reduceOnly'] = True
            # 交易所特定参数（OKX）
            if getattr(self, 'exchange_name', '') == 'OKX':
                params_extras['tdMode'] = order.margin_mode  # 'cross' 或 'isolated'
                params_extras['lever'] = actual_leverage
                # Prefer inline TP/SL when provided in order params
                try:
                    tp_px = order.extra_params.get('tpTriggerPx') or order.extra_params.get('takeProfitPrice')
                    sl_px = order.extra_params.get('slTriggerPx') or order.extra_params.get('stopLossPrice') or order.stop_price
                    if tp_px is not None:
                        params_extras['tpTriggerPx'] = self._format_price(ccxt_symbol, float(tp_px))
                    if sl_px is not None:
                        params_extras['slTriggerPx'] = self._format_price(ccxt_symbol, float(sl_px))
                except Exception:
                    pass

            if order.extra_params:
                try:
                    extras = dict(order.extra_params)
                    # 防止触发 OKX 批量下单端点
                    if getattr(self, 'exchange_name', '') == 'OKX':
                        for k in ['orders', 'algoOrders', 'batch', 'list']:
                            extras.pop(k, None)
                    params_extras.update(extras)
                except Exception:
                    pass

            logging.info(f"""
                Creating order:
                Symbol: {order.symbol}
                USDT Amount Intended: {order.amount}
                Leverage: {conversion_info['leverage']}x
                Coin Quantity: {quantity}
                Type: {type_arg}
                Side: {side_arg}
                Price: {price_arg}
                Stop Price: {params_extras.get('stopPrice')}
                Margin Mode: {order.margin_mode}
                Initial Margin: {conversion_info['initial_margin']} USDT
                Notional Value: {conversion_info['notional_value']} USDT
                """)

            # Execute order
            if getattr(self, 'exchange_name', '') == 'OKX':
                # 自动适配账户持仓模式
                pos_mode = getattr(self, 'pos_mode', None)
                if not params_extras.get('posSide'):
                    if pos_mode == 'long_short':
                        params_extras['posSide'] = 'long' if side_arg == 'buy' else 'short'
                    elif pos_mode == 'net':
                        params_extras.pop('posSide', None)

                raw = await self._okx_create_order(symbol_arg, type_arg, side_arg, amount_arg, price_arg, params_extras)
                code = str(raw.get('code'))
                if code != '0':
                    # 针对 51010 账户模式错误的回退
                    data_list_err = raw.get('data') or []
                    sCode = str((data_list_err[0] or {}).get('sCode')) if data_list_err else ''
                    if sCode == '51010':
                        pe = dict(params_extras)
                        pe.pop('posSide', None)
                        pe['tdMode'] = 'cross'
                        raw2 = await self._okx_create_order(symbol_arg, type_arg, side_arg, amount_arg, price_arg, pe)
                        if str(raw2.get('code')) == '0':
                            data_list = raw2.get('data') or []
                            result = data_list[0] if data_list else {}
                            raw = raw2
                        else:
                            pe['posSide'] = 'net'
                            raw3 = await self._okx_create_order(symbol_arg, type_arg, side_arg, amount_arg, price_arg, pe)
                            if str(raw3.get('code')) == '0':
                                data_list = raw3.get('data') or []
                                result = data_list[0] if data_list else {}
                                raw = raw3
                            else:
                                raise OrderException(f"OKX order rejected: {raw3}")
                    else:
                        raise OrderException(f"OKX order rejected: {raw}")
                else:
                    data_list = raw.get('data') or []
                    result = data_list[0] if data_list else {}
            else:
                result = await asyncio.to_thread(
                    self._exchange.createOrder,
                    symbol_arg,
                    type_arg,
                    side_arg,
                    amount_arg,
                    price_arg,
                    params_extras
                )

            if getattr(self, 'exchange_name', '') == 'OKX':
                order_id = result.get('ordId') or result.get('id')
                executed_price = float(result.get('px', 0) or use_price)
                raw_info = raw
            else:
                order_id = result['id']
                executed_price = float(result.get('price', 0) or use_price)
                raw_info = result
            return OrderResult(
                success=True,
                order_id=order_id,
                executed_price=executed_price,
                executed_amount=quantity,
                extra_info={
                    'raw_response': raw_info,
                    'conversion_info': conversion_info
                }
            )

        except Exception as e:
            logging.error(f"Error creating order: {e}")
            raise

    async def attach_tp_sl(self, symbol: str, side_open: str, executed_amount: float,
                           margin_mode: str, take_profit: Optional[float], stop_loss: Optional[float]) -> bool:
        try:
            if getattr(self, 'exchange_name', '') != 'OKX':
                return False
            # Determine closing side
            side_close = 'sell' if side_open.lower() == 'buy' else 'buy'
            # Determine posSide according to account mode
            pos_side = None
            if getattr(self, 'pos_mode', None) == 'long_short':
                pos_side = 'long' if side_open.lower() == 'buy' else 'short'
            # Contracts size must be integer
            amount_contracts = max(1, int(math.floor(executed_amount)))
            norm = self._normalize_symbol(symbol)
            raw = await self._okx_attach_tp_sl(norm, side_close, amount_contracts, margin_mode, pos_side, take_profit, stop_loss)
            ok = str(raw.get('code')) == '0'
            if not ok:
                logging.warning(f"OKX attach TP/SL failed: {raw}")
            return ok
        except Exception as e:
            logging.error(f"Error attaching TP/SL: {e}")
            return False


    def _format_price(self, symbol: str, price: float) -> float:
        """Format price according to symbol precision"""
        try:
            #TODO - market price
            # market_info = self._market_cache.get(symbol)
            # if market_info:
            #     precision = market_info.price_precision
            #     logging.info(f"format_price----price_precision---{market_info.price_precision}--{float(format(price, f'.{precision}f'))}---original--price{price}")
            #     return float(format(price, f'.{precision}f'))
            
            
            logging.info(f"format_priceoriginal--price{price}")
            return price
        except Exception as e:
            logging.error(f"Error formatting price: {e}")
            return price

    def _format_amount(self, symbol: str, amount: float) -> float:
        """Format amount according to symbol precision"""
        try:
            market_info = self._market_cache.get(symbol)
            if market_info:
                precision = market_info.amount_precision
                return float(format(amount, f'.{precision}f'))
            return amount
        except Exception as e:
            logging.error(f"Error formatting amount: {e}")
            return amount

class BinanceClient(ExchangeClient):
    """Binance exchange client implementation"""
    
    def __init__(self, credentials: ExchangeCredentials):
        super().__init__(credentials)
        self.min_request_interval = 0.05
        self.exchange_name = 'BINANCE'

    async def _setup_exchange(self) -> bool:
        """Setup Binance exchange connection"""
        try:
            logging.info(f"Setting up Binance with testnet={self.credentials.testnet}")
            # Configure Binance client for futures trading
            config = {
                'apiKey': self.credentials.api_key,
                'secret': self.credentials.api_secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,
                    'recvWindow': 60000,
                    'warnOnFetchOHLCVLimitArgument': False,
                    'createMarketBuyOrderRequiresPrice': False,
                    'sandboxMode': False,
                    # Avoid calling SAPI capital endpoints that require mainnet keys
                    'fetchCurrencies': False,
                }
            }

            if self.credentials.testnet:
                # ccxt deprecates futures testnet flag; override URLs instead
                # and keep options.testnet = False to avoid NotSupported error.
                config['options']['testnet'] = False
                config['urls'] = {
                    'api': {
                        # Route futures (fapi) requests to testnet domain
                        'fapi': 'https://testnet.binancefuture.com/fapi',
                        # Avoid SAPI where possible on testnet; keep defaultType=future
                        'public': 'https://testnet.binancefuture.com',
                        'private': 'https://testnet.binancefuture.com',
                    }
                }
                logging.info("Using Binance Futures Testnet via URL override")
            else:
                config['options']['testnet'] = False

            # Use the main Binance client with defaultType=future
            self._exchange = ccxt.binance(config)

            # Load markets first (futures-only) to avoid SAPI margin calls
            logging.info("Loading Binance markets (futures-only)...")
            await self._load_markets()
            logging.info("Binance markets loaded successfully")

            # Test connection after markets are loaded
            logging.info("Testing Binance connection...")
            await asyncio.to_thread(self._exchange.fetch_balance)
            logging.info("Binance connection test successful")

            return True
            
        except Exception as e:
            logging.error(f"Error setting up Binance exchange: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return False

    async def _load_markets(self) -> None:
        """Load Binance futures market data only"""
        try:
            if not self._exchange:
                raise ValueError("Exchange not initialized")

            self._rate_limit()
            # Restrict market loading to futures to avoid SAPI margin endpoints
            markets = await asyncio.to_thread(self._exchange.load_markets, False, {'type': 'future'})

            # Cache market info
            for symbol, market in markets.items():
                market_info = MarketInfo.from_exchange_market(market)
                if market_info:
                    self._market_cache[symbol] = market_info
                    self._market_cache_time[symbol] = time.time()

            logging.info(f"Loaded {len(self._market_cache)} Binance futures markets")

        except Exception as e:
            logging.error(f"Error loading Binance markets: {e}")
            raise
        
    async def get_leverage_brackets(self, symbol: str) -> List[Dict[str, Any]]:
        """Get leverage brackets"""
        try:
            self._rate_limit()
            response = await asyncio.to_thread(
                self._exchange.fapiPrivateGetLeverageBracket,
                {'symbol': symbol}
            )
            
            if response and isinstance(response, list):
                return [
                    {
                        'bracket': bracket['bracket'],
                        'initialLeverage': bracket['initialLeverage'],
                        'notionalCap': bracket['notionalCap'],
                        'notionalFloor': bracket['notionalFloor'],
                        'maintMarginRatio': bracket['maintMarginRatio']
                    }
                    for bracket in response[0]['brackets']
                ]
            return []
        except Exception as e:
            logging.error(f"Error getting leverage brackets: {e}")
            return []

    async def transfer_margin(self, symbol: str, amount: float, type: str) -> bool:
        """Transfer margin"""
        try:
            self._rate_limit()
            await asyncio.to_thread(
                self._exchange.fapiPrivatePostPositionMargin,
                {
                    'symbol': symbol,
                    'amount': amount,
                    'type': type  # 1: Add, 2: Reduce
                }
            )
            return True
        except Exception as e:
            logging.error(f"Error transferring margin: {e}")
            return False

class OKXClient(ExchangeClient):
    """OKX exchange client implementation"""
    
    def __init__(self, credentials: ExchangeCredentials):
        super().__init__(credentials)
        self.min_request_interval = 0.02
        self.exchange_name = 'OKX'
        self.pos_mode: Optional[str] = None  # 'net' 或 'long_short'

    async def _setup_exchange(self) -> bool:
        """Setup OKX exchange connection"""
        try:
            logging.info(f"Setting up OKX with testnet={self.credentials.testnet}")
            config = {
                'apiKey': self.credentials.api_key,
                'secret': self.credentials.api_secret,
                'password': self.credentials.passphrase,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'swap',
                    'adjustForTimeDifference': True
                }
            }
            
            if self.credentials.testnet:
                config['hostname'] = 'okx.com'  # Use main domain for testnet
                config['options']['testnet'] = True

            self._exchange = ccxt.okx(config)
            
            # Test connection
            logging.info("Testing OKX connection...")
            await asyncio.to_thread(self._exchange.fetch_balance)
            logging.info("OKX connection test successful")
            
            # Load markets
            logging.info("Loading OKX markets...")
            await self._load_markets()
            logging.info("OKX markets loaded successfully")

            try:
                cfg = await self._okx_request('/api/v5/account/config', 'GET', {})
                if cfg and cfg.get('code') == '0':
                    data = (cfg.get('data') or [{}])[0]
                    pm = str(data.get('posMode') or '').lower()
                    if pm.startswith('long'):
                        self.pos_mode = 'long_short'
                    elif pm.startswith('net'):
                        self.pos_mode = 'net'
                    logging.info(f"OKX position mode: {self.pos_mode}")
            except Exception as e:
                logging.warning(f"Unable to fetch OKX position mode: {e}")

            return True
            
        except Exception as e:
            logging.error(f"Error setting up OKX exchange: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return False
        
    async def get_leverage_brackets(self, symbol: str, margin_mode: str = 'cross') -> List[Dict[str, Any]]:
        """Get leverage brackets for OKX instrument
        Accepts ccxt symbol (e.g., BTC/USDT:USDT) and resolves to instId (e.g., BTC-USDT-SWAP).
        """
        try:
            self._rate_limit()
            norm = self._normalize_symbol(symbol)
            market = await asyncio.to_thread(self._exchange.market, norm)
            inst_id = market.get('id') or market.get('symbol') or norm
            response = await asyncio.to_thread(
                self._exchange.privateGetAccountMaxSize,
                {
                    'instId': inst_id,
                    'tdMode': str(margin_mode).lower()
                }
            )
            if response and response.get('code') == '0' and response.get('data'):
                return [
                    {
                        'maxLeverage': int(d.get('maxLever', 1)),
                        'maxSize': float(d.get('maxSz', 0) or 0),
                        'maintMarginRatio': float(d.get('mmr', 0) or 0)
                    }
                    for d in response.get('data', [])
                ]
            return []
        except Exception as e:
            logging.error(f"Error getting leverage brackets: {e}")
            return []

class ExchangeManager:
    """Exchange manager for multiple exchanges"""
    
    def __init__(self, config):
        self.config = config
        self.exchanges: Dict[str, ExchangeClient] = {}
        self.active_signals: Dict[str, TradingSignal] = {}
        self._monitoring = False
        self.monitor_interval = 1  # seconds
        
        # Cache
        self._position_cache: Dict[str, Dict[str, PositionInfo]] = {}
        self._position_cache_time: Dict[str, float] = {}
        self._balance_cache: Dict[str, AccountBalance] = {}
        self._balance_cache_time: Dict[str, float] = {}
        self.CACHE_DURATION = 5  # seconds

    async def initialize(self) -> bool:
        """Initialize all exchanges"""
        try:
            logging.info("Starting exchange initialization...")
            success = False  # Track if at least one exchange initialized

            # Initialize Binance according to USE_TESTNET and available keys
            try:
                if self.config.trading.use_testnet:
                    if self.config.exchange.binance_testnet_api_key:
                        logging.info("Initializing Binance (testnet)...")
                        binance_credentials = ExchangeCredentials(
                            api_key=self.config.exchange.binance_testnet_api_key,
                            api_secret=self.config.exchange.binance_testnet_api_secret,
                            testnet=True
                        )
                        binance_client = BinanceClient(binance_credentials)
                        if await binance_client.initialize():
                            self.exchanges['BINANCE'] = binance_client
                            success = True
                            logging.info("Binance (testnet) initialized successfully")
                        else:
                            logging.error("Failed to initialize Binance (testnet)")
                    else:
                        logging.warning("USE_TESTNET is true but BINANCE_TESTNET_API_KEY not set; skipping Binance")
                else:
                    if self.config.exchange.binance_api_key:
                        logging.info("Initializing Binance (mainnet)...")
                        binance_credentials = ExchangeCredentials(
                            api_key=self.config.exchange.binance_api_key,
                            api_secret=self.config.exchange.binance_api_secret,
                            testnet=False
                        )
                        binance_client = BinanceClient(binance_credentials)
                        if await binance_client.initialize():
                            self.exchanges['BINANCE'] = binance_client
                            success = True
                            logging.info("Binance (mainnet) initialized successfully")
                        else:
                            logging.error("Failed to initialize Binance (mainnet)")
                    else:
                        logging.warning("USE_TESTNET is false but BINANCE_API_KEY not set; skipping Binance")
            except Exception as e:
                logging.error(f"Error initializing Binance: {e}")
            
            # Initialize OKX if configured
            if self.config.exchange.okx_testnet_api_key:
                try:
                    logging.info("Initializing OKX...")
                    okx_credentials = ExchangeCredentials(
                        api_key=self.config.exchange.okx_testnet_api_key,
                        api_secret=self.config.exchange.okx_testnet_api_secret,
                        passphrase=self.config.exchange.okx_testnet_passphrase,
                        testnet=self.config.trading.use_testnet
                    )
                    okx_client = OKXClient(okx_credentials)
                    if await okx_client.initialize():
                        self.exchanges['OKX'] = okx_client
                        success = True
                        logging.info("OKX initialized successfully")
                    else:
                        logging.error("Failed to initialize OKX")
                except Exception as e:
                    logging.error(f"Error initializing OKX: {e}")

            if not success:
                logging.error("Failed to initialize any exchanges")
                return False

            logging.info(f"Exchange initialization complete. Active exchanges: {list(self.exchanges.keys())}")
            return True
            
        except Exception as e:
            logging.error(f"Error in exchange initialization: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return False
        
    async def cleanup(self):
        """Cleanup all exchanges"""
        for exchange in self.exchanges.values():
            await exchange.cleanup()
        self.exchanges.clear()
        self.active_signals.clear()

    async def execute_signal(self, signal: TradingSignal) -> OrderResult:
        """Execute trading signal with correct price handling for entry zones"""
        try:
            exchange = self.exchanges.get(signal.exchange)
            if not exchange:
                return OrderResult(
                    success=False,
                    error_message=f"Exchange {signal.exchange} not configured"
                )

            if signal.entry_zones:
                results = []
                total_amount = signal.position_size

                for zone in signal.entry_zones:
                    # Calculate USDT amount for this zone
                    zone_amount = total_amount * zone.percentage

                    logging.info(f"""
                        Processing Entry Zone:
                        Price: {zone.price}
                        Percentage: {zone.percentage}
                        USDT Amount: {zone_amount}
                        """)

                    order = OrderParams(
                        symbol=signal.symbol,
                        side=OrderSide.BUY if signal.action == 'OPEN_LONG' else OrderSide.SELL,
                        order_type=OrderType.LIMIT,  # 限价单
                        amount=zone_amount,
                        price=zone.price,  # 使用区间价格
                        leverage=signal.leverage,
                        margin_mode=signal.margin_mode,
                        extra_params={}
                    )

                    result = await exchange.create_order(order)
                    results.append(result)
                    
                    if result.success:
                        logging.info(f"Successfully created order for zone {zone.price}")
                        # 更新区间状态
                        zone.order_id = result.order_id
                        zone.status = 'PLACED'
                        tp_price = None
                        sl_price = signal.stop_loss
                        try:
                            if order.extra_params:
                                tp_price = order.extra_params.get('tpTriggerPx') or order.extra_params.get('takeProfitPrice') or tp_price
                                sl_price = order.extra_params.get('slTriggerPx') or order.extra_params.get('stopLossPrice') or sl_price
                        except Exception:
                            pass
                        try:
                            attached = await exchange.attach_tp_sl(
                                signal.symbol,
                                OrderSide.BUY if signal.action == 'OPEN_LONG' else OrderSide.SELL,
                                result.executed_amount,
                                signal.margin_mode,
                                tp_price,
                                sl_price
                            )
                            if attached:
                                logging.info("Attached TP/SL for zone order")
                            else:
                                logging.warning("Failed to attach TP/SL for zone order")
                        except Exception as e:
                            logging.error(f"Error attaching TP/SL: {e}")
                    else:
                        logging.error(f"Failed to create order for zone: {zone}")

                # Return first result (for compatibility)
                return results[0] if results else OrderResult(
                    success=False,
                    error_message="No orders created"
                )
            else:
                # Single entry price or market order
                order = OrderParams(
                    symbol=signal.symbol,
                    side=OrderSide.BUY if signal.action == 'OPEN_LONG' else OrderSide.SELL,
                    order_type=OrderType.MARKET if not signal.entry_price else OrderType.LIMIT,
                    amount=signal.position_size,
                    price=signal.entry_price,  # 可能为None（市价单）
                    leverage=signal.leverage,
                    margin_mode=signal.margin_mode,
                    extra_params={}
                )

                result = await exchange.create_order(order)
                if result.success:
                    tp_price = None
                    if signal.take_profit_levels:
                        tp_price = signal.take_profit_levels[0].price
                    sl_price = signal.stop_loss
                    try:
                        if order.extra_params:
                            tp_price = order.extra_params.get('tpTriggerPx') or order.extra_params.get('takeProfitPrice') or tp_price
                            sl_price = order.extra_params.get('slTriggerPx') or order.extra_params.get('stopLossPrice') or sl_price
                    except Exception:
                        pass
                    try:
                        attached = await exchange.attach_tp_sl(
                            signal.symbol,
                            OrderSide.BUY if signal.action == 'OPEN_LONG' else OrderSide.SELL,
                            result.executed_amount,
                            signal.margin_mode,
                            tp_price,
                            sl_price
                        )
                        if attached:
                            logging.info("Attached TP/SL for entry order")
                        else:
                            logging.warning("Failed to attach TP/SL for entry order")
                    except Exception as e:
                        logging.error(f"Error attaching TP/SL: {e}")
                return result

        except Exception as e:
            logging.error(f"Error executing signal: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return OrderResult(success=False, error_message=str(e))
        
    async def _check_take_profit_levels(self, exchange_name: str, symbol: str, position: PositionInfo) -> None:
        """Check and execute take profit orders"""
        try:
            # Get exchange client
            exchange = self.exchanges.get(exchange_name)
            if not exchange:
                return

            # Get signal
            signal_key = f"{exchange_name}_{symbol}"
            signal = self.active_signals.get(signal_key)
            if not signal or not signal.take_profit_levels:
                return

            # Get current price
            market_info = await exchange.get_market_info(symbol)
            if not market_info or not market_info.last_price:
                return

            current_price = market_info.last_price

            for tp_level in signal.take_profit_levels:
                if tp_level.is_hit:
                    continue

                # Check if TP is hit
                is_hit = (signal.action == 'OPEN_LONG' and current_price >= tp_level.price) or \
                        (signal.action == 'OPEN_SHORT' and current_price <= tp_level.price)

                if is_hit:
                    # Calculate close amount
                    close_amount = position.size * tp_level.percentage
                    
                    # Create take profit order
                    order = OrderParams(
                        symbol=symbol,
                        side=OrderSide.SELL if signal.action == 'OPEN_LONG' else OrderSide.BUY,
                        order_type=OrderType.MARKET,
                        amount=close_amount,
                        reduce_only=True,
                        extra_params={}
                    )

                    result = await exchange.create_order(order)
                    if result.success:
                        tp_level.is_hit = True
                        tp_level.hit_time = datetime.now()
                        logging.info(f"Take profit executed for {symbol} at {tp_level.price}")

        except Exception as e:
            logging.error(f"Error checking take profit levels: {e}")
             
    async def get_positions(self, exchange: Optional[str] = None) -> Dict[str, List[PositionInfo]]:
        """Get positions from all or specific exchange"""
        try:
            result = {}
            exchanges = [self.exchanges[exchange]] if exchange else self.exchanges.values()
            
            for ex in exchanges:
                positions = await ex.get_positions()
                if positions:
                    result[ex.exchange_name] = positions
                    
            return result
            
        except Exception as e:
            logging.error(f"Error getting positions: {e}")
            return {}

    async def get_open_orders(self, exchange: Optional[str] = None) -> Dict[str, List[OrderInfo]]:
        try:
            result: Dict[str, List[OrderInfo]] = {}
            exchanges = [self.exchanges[exchange]] if exchange else self.exchanges.values()
            for ex in exchanges:
                orders = await ex.get_open_orders()
                if orders:
                    result[ex.exchange_name] = orders
            return result
        except Exception as e:
            logging.error(f"Error getting open orders: {e}")
            return {}

    async def get_balances(self) -> Dict[str, AccountBalance]:
        """Get balances from all exchanges"""
        try:
            result = {}
            for name, exchange in self.exchanges.items():
                balance = await exchange.get_balance()
                if balance:
                    result[name] = balance
            return result
            
        except Exception as e:
            logging.error(f"Error getting balances: {e}")
            return {}

    async def close_position(self, exchange: str, symbol: str) -> OrderResult:
        """Close position"""
        try:
            exchange_client = self.exchanges.get(exchange)
            if not exchange_client:
                return OrderResult(
                    success=False,
                    error_message=f"Exchange {exchange} not configured"
                )
                
            # Get position
            positions = await exchange_client.get_positions(symbol)
            if not positions:
                return OrderResult(
                    success=False,
                    error_message=f"No position found for {symbol}"
                )
                
            position = positions[0]
            
            # Create close order
            order = OrderParams(
                symbol=symbol,
                side=OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY,
                order_type=OrderType.MARKET,
                amount=position.size,
                reduce_only=True
            )
            
            return await exchange_client.create_order(order)
            
        except Exception as e:
            logging.error(f"Error closing position: {e}")
            return OrderResult(success=False, error_message=str(e))

    async def modify_position(self, exchange: str, symbol: str,
                            stop_loss: Optional[float] = None,
                            take_profit: Optional[float] = None) -> bool:
        """Modify position stop loss and take profit"""
        try:
            exchange_client = self.exchanges.get(exchange)
            if not exchange_client:
                return False
                
            # Get position
            positions = await exchange_client.get_positions(symbol)
            if not positions:
                return False
                
            position = positions[0]
            success = True
            
            # Modify stop loss if provided
            if stop_loss:
                order = OrderParams(
                    symbol=symbol,
                    side=OrderSide.BUY if position.side == PositionSide.SHORT else OrderSide.SELL,
                    order_type=OrderType.STOP_MARKET,
                    amount=position.size,
                    stop_price=stop_loss,
                    reduce_only=True
                )
                result = await exchange_client.create_order(order)
                success = success and result.success
                
            # Modify take profit if provided
            if take_profit:
                order = OrderParams(
                    symbol=symbol,
                    side=OrderSide.BUY if position.side == PositionSide.SHORT else OrderSide.SELL,
                    order_type=OrderType.TAKE_PROFIT_MARKET,
                    amount=position.size,
                    stop_price=take_profit,
                    reduce_only=True
                )
                result = await exchange_client.create_order(order)
                success = success and result.success
                
            return success
            
        except Exception as e:
            logging.error(f"Error modifying position: {e}")
            return False

    async def get_leverage_brackets(self, exchange: str, symbol: str) -> List[Dict[str, Any]]:
        """Get leverage brackets"""
        try:
            exchange_client = self.exchanges.get(exchange)
            if not exchange_client:
                return []
                
            return await exchange_client.get_leverage_brackets(symbol)
            
        except Exception as e:
            logging.error(f"Error getting leverage brackets: {e}")
            return []

    async def get_funding_rates(self) -> Dict[str, Dict[str, float]]:
        """Get funding rates from all exchanges"""
        try:
            result = {}
            for name, exchange in self.exchanges.items():
                rates = {}
                for symbol in self._get_active_symbols(name):
                    rate = await exchange.get_funding_rate(symbol)
                    if rate is not None:
                        rates[symbol] = rate
                if rates:
                    result[name] = rates
            return result
            
        except Exception as e:
            logging.error(f"Error getting funding rates: {e}")
            return {}

    def _get_active_symbols(self, exchange: str) -> List[str]:
        """Get list of symbols with active positions"""
        try:
            active_symbols = set()
            for signal_key, signal in self.active_signals.items():
                if signal.exchange == exchange:
                    active_symbols.add(signal.symbol)
            return list(active_symbols)
            
        except Exception as e:
            logging.error(f"Error getting active symbols: {e}")
            return []

    async def get_market_info(self, exchange: str, symbol: str) -> Optional[MarketInfo]:
        """Get market information"""
        try:
            exchange_client = self.exchanges.get(exchange)
            if not exchange_client:
                return None
                
            return await exchange_client.get_market_info(symbol)
            
        except Exception as e:
            logging.error(f"Error getting market info: {e}")
            return None

    async def get_account_overview(self) -> Dict[str, Dict[str, Any]]:
        """Get account overview for all exchanges"""
        try:
            result = {}
            balances = await self.get_balances()
            positions = await self.get_positions()
            
            for exchange in self.exchanges:
                balance = balances.get(exchange, AccountBalance())
                exch_positions = positions.get(exchange, [])
                
                # Calculate account metrics
                used_margin = sum(pos.initial_margin for pos in exch_positions)
                margin_ratio = (used_margin / balance.total * 100) if balance.total > 0 else 0
                
                # Determine account health status
                if margin_ratio > 80:
                    health = 'CRITICAL'
                elif margin_ratio > 60:
                    health = 'WARNING'
                else:
                    health = 'HEALTHY'
                    
                result[exchange] = {
                    'total_equity': balance.total,
                    'used_margin': used_margin,
                    'available_margin': balance.free,
                    'margin_ratio': margin_ratio,
                    'unrealized_pnl': balance.unrealized_pnl,
                    'realized_pnl': balance.realized_pnl,
                    'account_health': health,
                    'total_positions': len(exch_positions),
                    'last_update': datetime.now()
                }
                
            return result
            
        except Exception as e:
            logging.error(f"Error getting account overview: {e}")
            return {}

    def calculate_position_value(self, position: PositionInfo) -> float:
        """Calculate position value in USDT"""
        try:
            return position.size * position.entry_price
        except Exception as e:
            logging.error(f"Error calculating position value: {e}")
            return 0.0

    def calculate_risk_metrics(self, position: PositionInfo) -> Dict[str, float]:
        """Calculate position risk metrics"""
        try:
            position_value = self.calculate_position_value(position)
            return {
                'position_value': position_value,
                'margin_ratio': (position.maintenance_margin / position_value * 100) if position_value > 0 else 0,
                'leverage_used': position_value / position.initial_margin if position.initial_margin > 0 else 0,
                'liquidation_distance': (abs(position.entry_price - position.liquidation_price) / position.entry_price * 100) 
                                      if position.liquidation_price else 0
            }
        except Exception as e:
            logging.error(f"Error calculating risk metrics: {e}")
            return {}
        
        

    async def _check_dynamic_stop_loss(self, exchange_name: str, symbol: str, position: PositionInfo) -> None:
        """Check and update dynamic stop loss"""
        try:
            if position.size == 0:
                return

            # Get signal for this position
            signal_key = f"{exchange_name}_{symbol}"
            signal = self.active_signals.get(signal_key)
            if not signal or not signal.dynamic_sl:
                return

            # Get market info
            exchange = self.exchanges.get(exchange_name)
            if not exchange:
                return

            market_info = await exchange.get_market_info(symbol)
            if not market_info or not market_info.last_price:
                return

            current_price = market_info.last_price
            entry_price = position.entry_price
            
            # Calculate new stop loss
            if signal.action == 'OPEN_LONG':
                if current_price > entry_price:
                    profit_distance = current_price - entry_price
                    new_sl = entry_price + (profit_distance * 0.5)  # Move stop loss to 50% of profit
                    if new_sl > signal.stop_loss:
                        await self.modify_position(
                            exchange_name,
                            symbol,
                            stop_loss=new_sl
                        )
            else:  # OPEN_SHORT
                if current_price < entry_price:
                    profit_distance = entry_price - current_price
                    new_sl = entry_price - (profit_distance * 0.5)
                    if new_sl < signal.stop_loss:
                        await self.modify_position(
                            exchange_name,
                            symbol,
                            stop_loss=new_sl
                        )

        except Exception as e:
            logging.error(f"Error checking dynamic stop loss: {e}")

    async def get_account_overview(self) -> Dict[str, Dict[str, Any]]:
        """Get account overview"""
        try:
            result = {}
            for exchange_name, exchange in self.exchanges.items():
                try:
                    balance = await exchange.fetch_balance()
                    positions = await exchange.fetch_positions()
                    
                    # Use correct field names
                    used_margin = balance.used_margin
                    margin_ratio = balance.margin_ratio
                    
                    # Determine account health status
                    if margin_ratio > 80:
                        health = 'CRITICAL'
                    elif margin_ratio > 60:
                        health = 'WARNING'
                    else:
                        health = 'HEALTHY'
                        
                    result[exchange_name] = {
                        'total_equity': balance.total_equity,
                        'used_margin': used_margin,
                        'available_margin': balance.free_margin,
                        'margin_ratio': margin_ratio,
                        'unrealized_pnl': balance.unrealized_pnl,
                        'realized_pnl': balance.realized_pnl,
                        'account_health': health,
                        'total_positions': len(positions),
                        'last_update': datetime.now()
                    }
                    
                except Exception as e:
                    logging.error(f"Error getting overview for {exchange_name}: {e}")
                    
            return result
            
        except Exception as e:
            logging.error(f"Error getting account overview: {e}")
            return {}
        
    async def _execute_take_profit(self, exchange_name: str, symbol: str, 
                                 position: PositionInfo, tp_level: TakeProfitLevel) -> None:
        """Execute take profit order"""
        try:
            exchange = self.exchanges.get(exchange_name)
            if not exchange:
                return

            # Calculate close amount
            close_amount = position.size * tp_level.percentage

            # Create market close order
            order = OrderParams(
                symbol=symbol,
                side='SELL' if position.side == PositionSide.LONG else 'BUY',
                order_type='MARKET',
                amount=close_amount,
                reduce_only=True
            )

            # Execute order
            result = await exchange.create_order(order)
            if result.success:
                tp_level.is_hit = True
                tp_level.hit_time = datetime.now()
                logging.info(f"Take profit executed for {symbol} at level {tp_level.price}")

        except Exception as e:
            logging.error(f"Error executing take profit: {e}")

    async def monitor_positions(self):
        """Monitor positions"""
        while True:
            try:
                for exchange_name, exchange in self.exchanges.items():
                    try:
                        positions = await exchange.fetch_positions()
                        for position in positions:
                            if position.size == 0:
                                continue
                                
                            # Check dynamic stop loss
                            await self._check_dynamic_stop_loss(exchange_name, position.symbol, position)
                            
                            # Check take profit targets
                            await self._check_take_profit_levels(exchange_name, position.symbol, position)
                            
                            # Update position stats
                            await self._update_position_stats(exchange_name, position.symbol, position)
                            
                    except Exception as e:
                        logging.error(f"Error monitoring positions for {exchange_name}: {e}")
                        continue
                        
            except Exception as e:
                logging.error(f"Error in position monitoring: {e}")
                
            await asyncio.sleep(1)
        

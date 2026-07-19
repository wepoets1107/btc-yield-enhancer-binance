"""
币安 API 客户端（测试网/主网）
替代 DeribitClient，提供相同的接口方法（balance、order、cancel等）

参考：
- https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info
- https://developers.binance.com/docs/derivatives/
"""

from __future__ import annotations

import time
import json
import hashlib
import hmac
import logging
import threading
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        v = float(value)
        if v != v:
            return default
        return v
    except (TypeError, ValueError):
        return default


class BinanceClient:
    """币安 REST API 客户端（同步、线程安全）"""

    MAINNET = "https://api.binance.com/api"
    TESTNET = "https://demo-api.binance.com/api"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")

        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.base_url = self.TESTNET if testnet else self.MAINNET
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 签名"""
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return signature

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    def _get(self, path: str, params: Optional[dict] = None, signed: bool = False) -> dict:
        """GET 请求（签名参数放 query string，严格排序）"""
        if params is None:
            params = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            query_sorted = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            sig = self._sign(params)
            url = f"{self.base_url}{path}?{query_sorted}&signature={sig}"
        else:
            url = f"{self.base_url}{path}"
            if params:
                url += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        try:
            resp = requests.get(url, headers=self._headers(), timeout=20)
            data = resp.json()
            if resp.status_code != 200:
                err_msg = data.get("msg", resp.text[:500])
                logger.error("Binance GET %s error %d: %s", path, resp.status_code, err_msg)
                return {"success": False, "error": err_msg}
            return {"success": True, "result": data}
        except requests.exceptions.RequestException as e:
            logger.error("Binance GET %s failed: %s", path, e)
            return {"success": False, "error": str(e)}

    def _post(self, path: str, params: dict) -> dict:
        """POST 请求（签名参数放 query string，严格排序，符合币安要求）"""
        params["timestamp"] = int(time.time() * 1000)
        # 签名：排序后的 query string
        query_sorted = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = self._sign(params)
        # URL：排序参数 + signature 在最后
        url = f"{self.base_url}{path}?{query_sorted}&signature={sig}"
        logger.debug("_post URL: %s", url[:250])
        try:
            resp = requests.post(url, headers=self._headers(), timeout=20)
            data = resp.json()
            if resp.status_code != 200:
                err_msg = data.get("msg", resp.text[:500])
                logger.error("Binance POST %s error %d: %s", path, resp.status_code, err_msg)
                return {"success": False, "error": err_msg}
            return {"success": True, "result": data}
        except requests.exceptions.RequestException as e:
            logger.error("Binance POST %s failed: %s", path, e)
            return {"success": False, "error": str(e)}

    def _delete(self, path: str, params: dict) -> dict:
        """DELETE 请求（签名参数放 query string，严格排序）"""
        params["timestamp"] = int(time.time() * 1000)
        query_sorted = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = self._sign(params)
        url = f"{self.base_url}{path}?{query_sorted}&signature={sig}"
        try:
            resp = requests.delete(url, headers=self._headers(), timeout=20)
            data = resp.json()
            if resp.status_code != 200:
                err_msg = data.get("msg", resp.text[:500])
                logger.error("Binance DELETE %s error %d: %s", path, resp.status_code, err_msg)
                return {"success": False, "error": err_msg}
            return {"success": True, "result": data}
        except requests.exceptions.RequestException as e:
            logger.error("Binance DELETE %s failed: %s", path, e)
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 连接检查
    # ------------------------------------------------------------------

    def check_connection(self) -> dict:
        """检查 API 连接"""
        result: dict = {
            "connected": False,
            "testnet": self.testnet,
            "auth_error": None,
        }
        # 先测连通性（无需密钥）
        try:
            r = requests.get(f"{self.base_url}/v3/ping", timeout=10)
            if r.status_code != 200:
                result["error"] = f"ping failed: {r.status_code}"
                return result
        except requests.exceptions.RequestException as e:
            result["error"] = str(e)
            return result

        # 再测鉴权
        r = self._get("/v3/account", signed=True)
        if r["success"]:
            result["connected"] = True
        else:
            result["auth_error"] = r.get("error")
        return result

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_index_price(self, index_name: str = "BTCUSDT") -> Optional[float]:
        """获取最新价格（币安无指数价格概念，使用最新成交价）"""
        result = self._get("/v3/ticker/price", {"symbol": index_name})
        if result["success"]:
            return _f(result["result"].get("price"))
        return None

    def get_instruments(self, currency: str = "BTC", kind: str = "spot") -> list[dict]:
        """获取交易品种列表（币安不需要kind参数）"""
        result = self._get("/v3/exchangeInfo")
        if not result["success"]:
            return []
        symbols = result["result"].get("symbols", [])
        # 过滤 BTC 现货交易对
        btc_spots = [
            s for s in symbols
            if s.get("quoteAsset") == "USDT" and s.get("baseAsset") == currency
            and s.get("status") == "TRADING"
        ]
        # 统一格式为 Deribit 兼容的字段名
        parsed = []
        for s in btc_spots:
            parsed.append({
                "instrument_name": s["symbol"],
                "contract_size": _f(s.get("contractSize", 1)),
                "min_trade_amount": _f(
                    next((f["minQty"] for f in s.get("filters", []) if f["filterType"] == "LOT_SIZE"), 0.0001)
                ),
                "step_size": _f(
                    next((f["stepSize"] for f in s.get("filters", []) if f["filterType"] == "LOT_SIZE"), 0.0001)
                ),
                "tick_size": _f(
                    next((f["tickSize"] for f in s.get("filters", []) if f["filterType"] == "PRICE_FILTER"), 0.01)
                ),
                "quote_asset": s.get("quoteAsset", "USDT"),
                "base_asset": s.get("baseAsset", "BTC"),
            })
        return parsed

    def get_ticker(self, instrument_name: str) -> Optional[dict]:
        """获取 ticker"""
        result = self._get("/v3/ticker/24hr", {"symbol": instrument_name})
        if result["success"]:
            return result["result"]
        return None

    # ------------------------------------------------------------------
    # K 线数据
    # ------------------------------------------------------------------

    def get_tradingview_chart_data(
        self,
        instrument_name: str,
        start_timestamp: int,
        end_timestamp: int,
        resolution: str = "1d",
    ) -> Optional[dict]:
        """获取 K 线数据（转换为 Deribit charts 格式）"""
        # 币安 resolution 映射: 5m/15m/30m/1h/4h/1d
        interval_map = {
            "1": "1m", "5": "5m", "15": "15m", "30": "30m",
            "60": "1h", "120": "2h", "240": "4h", "360": "6h",
            "720": "12h", "1D": "1d", "1W": "1w", "1M": "1M",
        }
        interval = interval_map.get(resolution, resolution)
        limit = 200  # 最多拉 200 根
        params = {
            "symbol": instrument_name,
            "interval": interval,
            "startTime": int(start_timestamp),
            "endTime": int(end_timestamp),
            "limit": limit,
        }
        result = self._get("/v3/klines", params)
        if not result["success"]:
            return None

        klines = result["result"]
        # 转换为 Deribit 格式：{ status, open[], close[], high[], low[], volume[], ticks[] }
        opens = []
        closes = []
        highs = []
        lows = []
        volumes = []
        ticks = []
        for k in klines:
            opens.append(_f(k[1]))
            highs.append(_f(k[2]))
            lows.append(_f(k[3]))
            closes.append(_f(k[4]))
            volumes.append(_f(k[5]))
            ticks.append(int(k[0]))
        return {
            "status": "ok",
            "open": opens,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "ticks": ticks,
        }

    # ------------------------------------------------------------------
    # 账户
    # ------------------------------------------------------------------

    def get_account_summary(self, currency: str = "USDT") -> Optional[dict]:
        """获取指定币种余额"""
        result = self._get("/v3/account", signed=True)
        if not result["success"]:
            return None
        balances = result["result"].get("balances", [])
        for b in balances:
            if b.get("asset") == currency:
                free = _f(b.get("free"))
                locked = _f(b.get("locked"))
                return {
                    "balance": free + locked,
                    "available": free,
                    "locked": locked,
                    "currency": currency,
                }
        return {"balance": 0.0, "available": 0.0, "locked": 0.0, "currency": currency}

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------

    def buy(
        self,
        instrument_name: str,
        amount: float,
        order_type: str = "market",
        label: Optional[str] = None,
        price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> dict:
        """买入限价/市价单"""
        params = {
            "symbol": instrument_name,
            "side": "BUY",
            "type": order_type.upper() if order_type in ("market", "limit") else order_type,
            "quantity": self._round_step(amount, instrument_name),
        }
        if order_type == "limit":
            if price is None:
                return {"success": False, "error": "price required for limit order"}
            params["price"] = self._round_price(price, instrument_name)
            params["timeInForce"] = "GTC"
        if post_only and order_type == "limit":
            # 币安 Demo 不支持 GTX，先用 GTC 代替。限价单本身就是 maker 偏好
            params["timeInForce"] = "GTC"

        # 币安没有 label，用 newClientOrderId
        if label:
            params["newClientOrderId"] = label[:32]  # 最多 32 字符

        result = self._post("/v3/order", params)
        if result["success"]:
            # 转换为 Deribit 风格结果
            return {"success": True, "result": self._to_order_result(result["result"])}
        return result

    def sell(
        self,
        instrument_name: str,
        amount: float,
        order_type: str = "market",
        label: Optional[str] = None,
        price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> dict:
        """卖出限价/市价单"""
        params = {
            "symbol": instrument_name,
            "side": "SELL",
            "type": order_type.upper() if order_type in ("market", "limit") else order_type,
            "quantity": self._round_step(amount, instrument_name),
        }
        if order_type == "limit":
            if price is None:
                return {"success": False, "error": "price required for limit order"}
            params["price"] = self._round_price(price, instrument_name)
            params["timeInForce"] = "GTC"
        if post_only and order_type == "limit":
            # 币安 Demo 不支持 GTX，用 GTC 代替
            params["timeInForce"] = "GTC"

        if label:
            params["newClientOrderId"] = label[:32]

        result = self._post("/v3/order", params)
        if result["success"]:
            return {"success": True, "result": self._to_order_result(result["result"])}
        return result

    def get_order_state(self, order_id: str) -> dict:
        """查询订单状态"""
        result = self._get("/v3/order", {
            "symbol": "BTCUSDT",  # 默认，调用前应确保 order_id 对应 BTCUSDT
            "orderId": int(order_id) if order_id.isdigit() else order_id,
        }, signed=True)
        if result["success"]:
            return {"success": True, "result": self._to_order_result(result["result"]),
                    "order_state": result["result"].get("status", "").lower()}
        return result

    def _get_order_symbol(self, order_id: str) -> str:
        """从订单 ID 推断交易对（简化：默认 BTCUSDT，由引擎配置决定）"""
        return "BTCUSDT"

    def cancel_order(self, order_id: str) -> dict:
        """取消单笔订单"""
        params = {
            "symbol": "BTCUSDT",
            "orderId": int(order_id) if order_id.isdigit() else order_id,
        }
        result = self._delete("/v3/order", params)
        return result

    def cancel_all_by_instrument(self, instrument_name: str) -> dict:
        """取消指定交易对的所有订单"""
        result = self._delete("/v3/openOrders", {"symbol": instrument_name})
        return result

    def get_open_orders(self, instrument_name: Optional[str] = None) -> list[dict]:
        """获取未成交订单"""
        params = {}
        if instrument_name:
            params["symbol"] = instrument_name
        result = self._get("/v3/openOrders", params, signed=True)
        if not result["success"]:
            return []
        parsed = []
        for o in result["result"]:
            parsed.append({
                "order_id": str(o.get("orderId", "")),
                "direction": o.get("side", "").lower(),
                "price": _f(o.get("price")),
                "amount": _f(o.get("origQty")),
                "filled_amount": _f(o.get("executedQty")),
                "order_state": o.get("status", "").lower(),
                "instrument_name": o.get("symbol", instrument_name or ""),
                "label": o.get("clientOrderId", ""),
                "creation_timestamp": o.get("time", 0),
            })
        return parsed

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _round_step(value: float, instrument_name: str) -> str:
        """四舍五入到交易对允许的数量精度（简化：6位小数）"""
        return f"{value:.6f}"

    @staticmethod
    def _round_price(price: float, instrument_name: str) -> str:
        """四舍五入到交易对允许的价格精度（简化：2位小数）"""
        return f"{price:.2f}"

    @staticmethod
    def _to_order_result(order_data: dict) -> dict:
        """将币安订单响应转换为 Deribit 风格的 order dict"""
        side = order_data.get("side", "").lower()
        # 判断买卖方向
        direction = "buy" if side == "buy" else "sell"
        filled_qty = _f(order_data.get("executedQty"))
        cum_quote = _f(order_data.get("cummulativeQuoteQty"))
        avg_price = cum_quote / filled_qty if filled_qty > 0 else _f(order_data.get("price"))
        return {
            "order_id": str(order_data.get("orderId", "")),
            "order_state": order_data.get("status", "").lower(),
            "filled_amount": filled_qty,
            "average_price": avg_price,
            "total_cost": cum_quote,
            "direction": direction,
            "instrument_name": order_data.get("symbol", ""),
            "label": order_data.get("clientOrderId", ""),
            "original": order_data,
        }

    @staticmethod
    def parse_order_result(result_data: dict) -> dict:
        """解析订单结果（兼容 Deribit 接口）"""
        return {
            "order_id": result_data.get("order_id", ""),
            "state": result_data.get("order_state", ""),
            "filled_amount": _f(result_data.get("filled_amount")),
            "average_price": _f(result_data.get("average_price")),
            "total_cost": _f(result_data.get("total_cost")),
            "label": result_data.get("label", ""),
            "direction": result_data.get("direction", ""),
            "instrument_name": result_data.get("instrument_name", ""),
        }

"""
BTC 收益增强策略回测 v1.0

回测参数：
- 投入: 10,000 USDT
- 持仓: 一半(5,000)换成BTC，另一半(5,000)USDT
- 单笔: 100 USDT
- 保险: 策略启动时买0.1张 BTC ATM 1年期 Put
- 数据: 币安现货 K线 + Deribit 期权数据
- 周期: 过去1年(5分钟K线)
- 手续费: 万3(0.03%)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import argparse

import requests

BJT = timezone(timedelta(hours=8))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# =========================================================================
# 数据获取
# =========================================================================

def fetch_binance_klines(symbol: str, start_ts: int, end_ts: int, interval: str = "5m") -> list[dict]:
    """从币安公共 API 分批拉取 K 线数据"""
    all_klines = []
    limit = 1000
    current_start = start_ts

    total_needed = (end_ts - start_ts) // 300000  # 5分=300秒
    print(f"  需拉取约 {total_needed} 根 K 线...")

    while current_start < end_ts:
        try:
            r = requests.get("https://api.binance.com/api/v3/klines", params={
                "symbol": symbol, "interval": interval,
                "startTime": current_start, "endTime": end_ts, "limit": limit,
            }, timeout=30)
            data = r.json()
            if not isinstance(data, list) or len(data) == 0:
                break
            all_klines.extend(data)
            current_start = data[-1][0] + 1  # 最后一根的时间戳+1
            if len(data) < limit:
                break
            print(f"    已拉取 {len(all_klines)} 根...")
        except Exception as e:
            print(f"    API 请求失败: {e}, 重试中...")
            time.sleep(2)
            continue

    # 转换为统一格式
    result = []
    for k in all_klines:
        result.append({
            "timestamp": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return result


def get_option_atm_strike(timestamp: int) -> int:
    """获取给定时间点的BTC价格, 取整到最近500的ATM行权价"""
    try:
        r = requests.post("https://www.deribit.com/api/v2/", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "public/get_index_price",
            "params": {"index_name": "btc_usdc"},
        }, timeout=10)
        price = r.json()["result"]["index_price"]
        return int(round(price / 500) * 500)
    except Exception:
        return 65000  # fallback


def _norm_cdf(x):
    """标准正态分布CDF（无需 scipy）"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def estimate_option_premium(spot: float, strike: float, dte: int, iv: float = 0.60) -> float:
    """Black-Scholes 估计看跌期权价格"""
    T = dte / 365.0
    if T <= 0:
        return max(0, strike - spot)
    rate = 0.02
    d1 = (math.log(spot / strike) + (rate + iv * iv / 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    price = strike * math.exp(-rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(price, 0)


def bs_put_price(spot: float, strike: float, tte_days: float, iv: float, rate: float = 0.02) -> float:
    """Black-Scholes 看跌期权重新定价"""
    if tte_days <= 0:
        return max(0, strike - spot)
    T = tte_days / 365.0
    if T <= 0 or iv <= 0:
        return max(0, strike - spot)
    try:
        d1 = (math.log(spot / strike) + (rate + iv * iv / 2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        price = strike * math.exp(-rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        return max(price, 0)
    except (ZeroDivisionError, ValueError):
        return max(0, strike - spot)


# =========================================================================
# 回测引擎
# =========================================================================

class BacktestEngine:
    """策略回测引擎"""

    def __init__(self, klines: list[dict], initial_usdt: float = 10000,
                 trade_size: float = 100, fee_rate: float = 0.0003,
                 rv_min: float = 0.005, rv_max: float = 0.05,
                 cooldown_candles: int = 6):  # 30s/poll × 6 = 3min冷却
        self.klines = klines
        self.initial_usdt = initial_usdt
        self.trade_size = trade_size
        self.fee_rate = fee_rate
        self.rv_min = rv_min
        self.rv_max = rv_max
        self.cooldown_candles = cooldown_candles

        # 初始持仓
        spot_btc = initial_usdt / 2 / klines[0]["open"]
        self.usdt = initial_usdt / 2
        self.btc = spot_btc

        self.anchor = klines[0]["open"]
        self.daily_rv = rv_min
        self.upper_thresh = 0.0
        self.lower_thresh = 0.0
        self._recalc_thresholds()

        # 状态
        self.cooldown_remaining = 0
        self.total_trades = 0
        self.trades_log: list[dict] = []
        self.portfolio_values: list[dict] = []
        self.pending_buy = False
        self.pending_sell = False

        # 期权
        self.put_strike: float = 0
        self.put_cost: float = 0
        self.put_notional: float = 0.1
        self.put_dte: float = 365
        self._put_expiry_ts: int = 0      # 到期时间戳(ms)

        print(f"初始化: USDT={self.usdt:.2f} BTC={self.btc:.6f} 锚={self.anchor:.0f}")

    def _recalc_thresholds(self):
        self.upper_thresh = round(self.anchor * (1 + self.daily_rv))
        self.lower_thresh = round(self.anchor * (1 - self.daily_rv))

    def _calc_rv(self, kline_idx: int) -> float:
        """从最近12根(1小时)计算RMS日化RV"""
        start = max(0, kline_idx - 12)
        n = kline_idx - start
        if n < 12:
            return self.rv_min
        sq_sum = 0.0
        for i in range(start, kline_idx):
            op = self.klines[i]["open"]
            cl = self.klines[i]["close"]
            if op > 0:
                r = (cl - op) / op
                sq_sum += r * r
        rv_hour = math.sqrt(sq_sum / n)
        rv_daily = rv_hour * math.sqrt(24)
        return max(self.rv_min, min(self.rv_max, rv_daily))

    def _check_fill(self, candle: dict, side: str) -> bool:
        """检查价格是否穿越阈值（成交）"""
        low, high = candle["low"], candle["high"]
        if side == "buy":
            # 价格跌破下阈值 → 买入成交
            return low <= self.lower_thresh
        elif side == "sell":
            # 价格涨破上阈值 → 卖出成交
            return high >= self.upper_thresh
        return False

    def run(self):
        """运行回测"""
        print(f"\n开始回测: {len(self.klines)} 根K线")
        print(f"{'='*70}")

        for i, candle in enumerate(self.klines):
            ts = candle["timestamp"]
            price = candle["close"]

            # 1. 更新 RV（每小时，即每12根K线）
            if i % 12 == 0 and i >= 12:
                new_rv = self._calc_rv(i)
                if new_rv != self.daily_rv:
                    old_rv = self.daily_rv
                    self.daily_rv = new_rv
                    self._recalc_thresholds()

            # 2. 冷却递减
            if self.cooldown_remaining > 0:
                self.cooldown_remaining -= 1

            # 3. 检查买入成交
            if not self.pending_buy and not self.pending_sell:
                # 没有挂单 → 检查是否需要挂单
                if self._check_fill(candle, "buy") and self.usdt >= self.trade_size:
                    self._execute_buy(candle)
                elif self._check_fill(candle, "sell") and self.btc >= self.trade_size / price:
                    self._execute_sell(candle)
                else:
                    # 方案A: 锚点偏离超过RV → 重置锚点
                    deviation = abs(price / self.anchor - 1)
                    if deviation > self.daily_rv:
                        old_anchor = self.anchor
                        self.anchor = price
                        self._recalc_thresholds()
                        self.cooldown_remaining = self.cooldown_candles

            # 4. 记录组合价值
            self._record_portfolio(candle)

            # 进度
            if i > 0 and i % 20000 == 0:
                pct = i / len(self.klines) * 100
                print(f"  进度 {pct:.0f}% ({i}/{len(self.klines)}) 交易={self.total_trades}")

        print(f"\n回测完成! 共 {self.total_trades} 笔交易")
        return self._calc_metrics()

    def _execute_buy(self, candle: dict):
        """执行买入 $100"""
        price = self.lower_thresh  # 在阈值成交
        btc_amount = self.trade_size / price
        fee = btc_amount * self.fee_rate
        cost = self.trade_size + self.trade_size * self.fee_rate

        if self.usdt >= cost:
            self.usdt -= cost
            self.btc += btc_amount - fee
            self.anchor = price
            self._recalc_thresholds()
            self.cooldown_remaining = self.cooldown_candles
            self.total_trades += 1
            self.trades_log.append({
                "time": datetime.fromtimestamp(candle["timestamp"]//1000, tz=BJT).isoformat(),
                "side": "buy", "price": price, "amount": round(btc_amount, 6),
                "fee": round(fee, 8), "usdt_after": round(self.usdt, 2),
                "btc_after": round(self.btc, 6),
            })

    def _execute_sell(self, candle: dict):
        """执行卖出 $100"""
        price = self.upper_thresh
        btc_amount = self.trade_size / price
        fee = btc_amount * self.fee_rate
        btc_to_sell = btc_amount + fee

        if self.btc >= btc_to_sell:
            self.btc -= btc_to_sell
            self.usdt += self.trade_size - self.trade_size * self.fee_rate
            self.anchor = price
            self._recalc_thresholds()
            self.cooldown_remaining = self.cooldown_candles
            self.total_trades += 1
            self.trades_log.append({
                "time": datetime.fromtimestamp(candle["timestamp"]//1000, tz=BJT).isoformat(),
                "side": "sell", "price": price, "amount": round(btc_amount, 6),
                "fee": round(fee, 8), "usdt_after": round(self.usdt, 2),
                "btc_after": round(self.btc, 6),
            })

    def _record_portfolio(self, candle: dict):
        price = candle["close"]
        strategy_total = self.usdt + self.btc * price

        # 期权重定价
        option_value = 0.0
        if self._put_expiry_ts > 0:
            tte_days = max(0, (self._put_expiry_ts - candle["timestamp"]) / 86400000.0)
            if tte_days > 0 and self.put_strike > 0:
                iv_est = 0.50  # 远期IV略低
                option_value = bs_put_price(price, self.put_strike, tte_days, iv_est) * self.put_notional
            elif tte_days <= 0:
                # 到期: 价内就值钱
                option_value = max(0, self.put_strike - price) * self.put_notional

        grand_total = strategy_total + option_value

        self.portfolio_values.append({
            "timestamp": candle["timestamp"],
            "datetime": datetime.fromtimestamp(candle["timestamp"]//1000, tz=BJT).isoformat(),
            "usdt": round(self.usdt, 2),
            "btc": round(self.btc, 6),
            "btc_value": round(self.btc * price, 2),
            "strategy_total": round(strategy_total, 2),
            "option_value": round(option_value, 2),
            "total": round(grand_total, 2),
            "price": price,
        })

    # =====================================================================
    # 指标计算
    # =====================================================================

    def _calc_metrics(self) -> dict:
        pv = self.portfolio_values
        if len(pv) < 2:
            return {"error": "数据不足"}

        initial_total = self.initial_usdt
        final_total = pv[-1]["total"]
        total_return = (final_total / initial_total - 1)

        # 日收益率序列（按天聚合）
        daily_returns = {}
        for v in pv:
            d = v["datetime"][:10]
            if d not in daily_returns:
                daily_returns[d] = v["total"]
        daily_dates = sorted(daily_returns.keys())
        daily_ret = []
        for i in range(1, len(daily_dates)):
            r = daily_returns[daily_dates[i]] / daily_returns[daily_dates[i-1]] - 1
            daily_ret.append(r)

        if len(daily_ret) < 5:
            print("警告: 日收益率数据不足, 改为用全部K线计算")

        ret_array = daily_ret
        mean_ret = sum(ret_array) / len(ret_array) if ret_array else 0
        variance = sum((r - mean_ret) ** 2 for r in ret_array) / (len(ret_array) - 1) if len(ret_array) > 1 else 0
        std_ret = math.sqrt(variance)

        # 年化收益
        n_days = len(daily_ret)
        ann_return = (1 + mean_ret) ** 365 - 1 if n_days > 0 else 0

        # 夏普比率 (无风险利率 2%)
        rf_daily = 0.02 / 365
        excess = mean_ret - rf_daily
        sharpe = (excess / std_ret * math.sqrt(365)) if std_ret > 0 else 0

        # 最大回撤
        peak = initial_total
        max_drawdown = 0
        max_drawdown_end = 0
        for v in pv:
            if v["total"] > peak:
                peak = v["total"]
            dd = (peak - v["total"]) / peak
            if dd > max_drawdown:
                max_drawdown = dd
                max_drawdown_end = v["timestamp"]

        # 卡玛比率
        calmar = ann_return / max_drawdown if max_drawdown > 0 else 0

        # 胜率
        if len(self.trades_log) > 0:
            wins = sum(1 for t in self.trades_log if t["side"] == "sell" and t["price"] > 0)
            # 简化: 卖出价 > 上一次买入价才算赢
            winning_trades = 0
            total_completed = 0
            buy_prices = []
            for t in self.trades_log:
                if t["side"] == "buy":
                    buy_prices.append(t["price"])
                elif t["side"] == "sell" and buy_prices:
                    avg_buy = buy_prices[-1] if buy_prices else 0
                    if t["price"] > avg_buy:
                        winning_trades += 1
                    total_completed += 1
            win_rate = winning_trades / total_completed if total_completed > 0 else 0
        else:
            win_rate = 0

        # 交易统计
        total_buys = sum(1 for t in self.trades_log if t["side"] == "buy")
        total_sells = sum(1 for t in self.trades_log if t["side"] == "sell")
        total_fees = sum(t["fee"] for t in self.trades_log)
        avg_trade_per_day = len(self.trades_log) / max(n_days, 1)

        return {
            "初使总资产": f"${initial_total:,.0f}",
            "最终总资产": f"${final_total:,.0f}",
            "总收益率": f"{total_return*100:.2f}%",
            "年化收益率": f"{ann_return*100:.2f}%",
            "夏普比率": f"{sharpe:.2f}",
            "最大回撤": f"{max_drawdown*100:.2f}%",
            "卡玛比率": f"{calmar:.2f}",
            "胜率": f"{win_rate*100:.1f}%",
            "总交易笔数": self.total_trades,
            "买入次数": total_buys,
            "卖出次数": total_sells,
            "总手续费": f"${total_fees:.2f}",
            "日均交易": f"{avg_trade_per_day:.1f}次",
            "回测天数": n_days,
            "K线总数": len(pv),
        }


# =========================================================================
# 主程序
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="BTC 收益增强策略回测")
    parser.add_argument("--start", help="回测起始日期 YYYY-MM-DD", default="2025-07-18")
    parser.add_argument("--end", help="回测结束日期 YYYY-MM-DD", default="2026-07-18")
    parser.add_argument("--initial", type=float, default=10000, help="初始资金 USDT")
    parser.add_argument("--trade-size", type=float, default=100, help="单笔交易额 USDT")
    parser.add_argument("--fee", type=float, default=0.0003, help="手续费率")
    parser.add_argument("--no-option", action="store_true", help="不加期权保险")
    parser.add_argument("--output", help="输出目录", default=CURRENT_DIR)
    args = parser.parse_args()

    print("=" * 60)
    print("BTC 收益增强策略回测 v1.0")
    print("=" * 60)
    print(f"\n回测区间: {args.start} ~ {args.end}")
    print(f"初始资金: ${args.initial:,}")
    print(f"初始持仓: $5,000 USDT + $5,000 BTC")
    print(f"单笔交易: ${args.trade_size:,}")
    print(f"手续费率: {args.fee*100:.2f}%")
    print(f"期权保险: {'否' if args.no_option else '0.1张 ATM 1年期 Put'}")

    # 1. 拉取数据
    print(f"\n[1/3] 拉取币安 BTCUSDT 5分钟 K 线数据...")
    start_dt = datetime.strptime(args.start + " 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    end_dt = datetime.strptime(args.end + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)

    cache_file = os.path.join(args.output, f"klines_cache_{args.start}_{args.end}.json")
    if os.path.exists(cache_file):
        print("  发现缓存文件, 直接加载...")
        with open(cache_file, "r") as f:
            klines = json.load(f)
    else:
        klines = fetch_binance_klines("BTCUSDT", start_ts, end_ts)
        if klines:
            with open(cache_file, "w") as f:
                json.dump(klines, f)
            print(f"  已缓存 {len(klines)} 根K线到 {cache_file}")

    if not klines:
        print("❌ 数据拉取失败")
        return

    print(f"  共 {len(klines)} 根K线 | 首根: {klines[0]['timestamp']} 末根: {klines[-1]['timestamp']}")

    # 2. 运行回测
    print(f"\n[2/3] 运行回测引擎...")
    engine = BacktestEngine(
        klines=klines,
        initial_usdt=args.initial,
        trade_size=args.trade_size,
        fee_rate=args.fee,
    )

    # 期权保险
    if not args.no_option:
        start_price = klines[0]["open"]
        atm_strike = round(start_price / 500) * 500
        iv_high = 0.60  # 1年远期IV较高
        option_premium = estimate_option_premium(start_price, atm_strike, 365, iv_high)
        put_cost = option_premium * engine.put_notional
        engine.put_strike = atm_strike
        engine.put_cost = put_cost
        # 到期日: 回测起始 + 365天
        expiry_dt = start_dt + timedelta(days=365)
        engine._put_expiry_ts = int(expiry_dt.timestamp() * 1000)
        # 初始扣除保费
        engine.usdt -= put_cost
        print(f"\n  期权保险:")
        print(f"    ATM Strike: ${atm_strike:,}")
        print(f"    隐含波动率: {iv_high*100:.0f}%")
        print(f"    1张保费: ${option_premium:,.0f}")
        print(f"    0.1张保费: ${put_cost:,.0f} ({put_cost/args.initial*100:.1f}%本金)")
        print(f"    到期日: 2026-07 (1年)")

    metrics = engine.run()

    # 3. 输出结果
    print(f"\n[3/3] 结果汇总")
    print("=" * 60)
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    # 保存交易日志
    trade_file = os.path.join(args.output, "backtest_trades.csv")
    with open(trade_file, "w") as f:
        f.write("time,side,price,amount,fee,usdt_after,btc_after\n")
        for t in engine.trades_log:
            f.write(f"{t['time']},{t['side']},{t['price']},{t['amount']},{t['fee']},{t['usdt_after']},{t['btc_after']}\n")
    print(f"\n交易日志: {trade_file}")

    # 保存组合价值序列
    pv_file = os.path.join(args.output, "backtest_portfolio.csv")
    with open(pv_file, "w") as f:
        f.write("datetime,usdt,btc,btc_value,total,price\n")
        for v in engine.portfolio_values:
            f.write(f"{v['datetime']},{v['usdt']},{v['btc']},{v['btc_value']},{v['total']},{v['price']}\n")
    print(f"组合价值序列: {pv_file}")

    # 简单对比: 纯持币 vs 纯持U
    start_price = klines[0]["open"]
    end_price = klines[-1]["close"]
    hold_btc_value = args.initial / start_price * end_price  # 全部买BTC
    hold_usdt = args.initial  # 全部持U
    print(f"\n  对比基准:")
    print(f"    全部持BTC: ${hold_btc_value:,.0f} ({((hold_btc_value/args.initial-1)*100):.2f}%)")
    print(f"    全部持USDT: ${hold_usdt:,.0f} (0%)")
    print(f"    策略总资产: ${float(metrics['最终总资产'].replace('$','').replace(',','')):,.0f}")


if __name__ == "__main__":
    main()

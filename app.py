"""
BTC 收益增强策略 - 币安交易所版 Flask Web 应用

用法：
    export BINANCE_API_KEY=xxx
    export BINANCE_API_SECRET=xxx
    export BINANCE_TESTNET=1
    python app_binance.py

或创建 .env 文件：
    BINANCE_API_KEY=xxx
    BINANCE_API_SECRET=xxx
    BINANCE_TESTNET=1

前端：http://localhost:5052/
"""

from __future__ import annotations

import os
import json
import logging
import sys
import threading
import time as pytime

from flask import Flask, jsonify, request, make_response
from flask_sock import Sock

from strategy_engine import StrategyEngine
from binance_api import BinanceClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")


def _load_env_file():
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

# ---------------------------------------------------------------------------
# 币安 API 凭证（环境变量）
# ---------------------------------------------------------------------------
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
USE_TESTNET = os.environ.get("BINANCE_TESTNET", "1") == "1"
EXCHANGE = os.environ.get("EXCHANGE", "binance").lower()
BINANCE_PORT = int(os.environ.get("BINANCE_PORT", "5052"))

if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    print("请设置环境变量 BINANCE_API_KEY 和 BINANCE_API_SECRET")
    print("或在 .env 文件中添加:")
    print("  BINANCE_API_KEY=xxx")
    print("  BINANCE_API_SECRET=xxx")
    print("  BINANCE_TESTNET=1 (可选，默认测试网)")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Flask + WebSocket
# ---------------------------------------------------------------------------
app = Flask(__name__, static_url_path='/static', static_folder='static')
sock = Sock(app)

engine: StrategyEngine = None
engine_lock = threading.Lock()
ws_clients = set()
ws_clients_lock = threading.Lock()


def broadcast_state(state: dict):
    payload = json.dumps(state, ensure_ascii=False, default=str)
    with ws_clients_lock:
        clients = list(ws_clients)
    dead = []
    for client in clients:
        try:
            client.send(payload)
        except Exception:
            dead.append(client)
    if dead:
        with ws_clients_lock:
            for c in dead:
                ws_clients.discard(c)


@sock.route("/ws")
def ws_handler(ws):
    with ws_clients_lock:
        ws_clients.add(ws)
    logger.info("WebSocket client connected (%d total)", len(ws_clients))
    if engine:
        try:
            state = engine.get_state()
            ws.send(json.dumps(state, ensure_ascii=False, default=str))
        except Exception:
            pass
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
    except Exception:
        pass
    finally:
        with ws_clients_lock:
            ws_clients.discard(ws)


def on_state_update(state: dict):
    broadcast_state(state)


def _create_api_client():
    """创建币安 API 客户端"""
    logger.info("Creating Binance API client (testnet=%s)", USE_TESTNET)
    return BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=USE_TESTNET)


def _create_engine(state_callback=None):
    api = _create_api_client()
    engine_config = {
        "instrument_name": "BTCUSDT",
        "index_name": "BTCUSDT",
        "trade_size_usdc": 100.0,
        "rv_min": 0.005,
        "rv_max": 0.05,
        "poll_interval": 30,
        "cooldown_seconds": 180,
        "min_poll_balance_usdc": 200,
    }
    return StrategyEngine(
        api=api,
        config=engine_config,
        state_callback=state_callback or on_state_update,
    )


# ---------------------------------------------------------------------------
# API 路由（与 Deribit 版保持一致）
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/status")
def api_status():
    if engine is None:
        return jsonify({"error": "Strategy engine not initialized"}), 503
    with engine_lock:
        state = engine.get_state()
    return jsonify(state)


@app.route("/api/init", methods=["POST"])
def api_init():
    global engine
    with engine_lock:
        if engine and engine._running:
            return jsonify({"success": True, "message": "Engine already running", "status": engine.status})
        engine = _create_engine()
        if not engine.initialize():
            return jsonify({"success": False, "message": "Initialization failed"}), 500
    for _ in range(20):
        if engine.status == "ready":
            break
        pytime.sleep(0.5)
    return jsonify({"success": True, "message": "Engine initialized", "status": engine.status})


@app.route("/api/start", methods=["POST"])
def api_start():
    global engine
    with engine_lock:
        if engine is None or not engine._running:
            engine = _create_engine()
            if not engine.initialize():
                return jsonify({"success": False, "message": "Initialization failed"}), 500
        if engine._trading_enabled:
            return jsonify({"success": False, "message": "Already trading"})
        success = engine.start()
    return jsonify({
        "success": success,
        "message": "Trading started" if success else "Failed to start trading",
    })


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if engine is None:
        return jsonify({"error": "No strategy running"}), 400
    engine.stop()
    return jsonify({"success": True, "message": "Strategy stopping"})


@app.route("/api/params", methods=["GET", "POST"])
def api_params():
    global engine
    if request.method == "GET":
        if engine is None:
            return jsonify({"editable": False, "message": "Engine not initialized"}), 503
        with engine_lock:
            cfg = engine.cfg
            return jsonify({
                "editable": True,
                "anchor_price": engine.anchor_price,
                "trade_size_usdc": cfg["trade_size_usdc"],
                "rv_min": cfg["rv_min"],
                "rv_max": cfg["rv_max"],
                "rv_update_interval_minutes": cfg.get("rv_update_interval_minutes", 60),
                "poll_interval": cfg["poll_interval"],
                "cooldown_seconds": cfg.get("cooldown_seconds", 60),
                "min_poll_balance_usdc": cfg["min_poll_balance_usdc"],
            })
    if engine is None:
        return jsonify({"error": "Engine not initialized"}), 503
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data"}), 400
    changed = []
    with engine_lock:
        for key, (lo, hi) in [("poll_interval", (5, 300)), ("cooldown_seconds", (10, 600)),
                               ("rv_update_interval_minutes", (5, 1440))]:
            if key in data:
                val = int(data[key])
                if lo <= val <= hi:
                    engine.cfg[key] = val
                    changed.append(f"{key}={val}")
        for key, (lo, hi) in [("trade_size_usdc", (10, 10000)), ("rv_min", (0.0001, 0.05)),
                               ("rv_max", (0.001, 0.1)), ("min_poll_balance_usdc", (10, 10000))]:
            if key in data:
                val = float(data[key])
                if lo <= val <= hi:
                    engine.cfg[key] = val
                    changed.append(f"{key}={val}")
        if "anchor_price" in data:
            val = float(data["anchor_price"])
            if val > 0:
                engine.anchor_price = val
                engine._recalc_thresholds()
                changed.append(f"anchor=${val:.2f}")
    if changed:
        logger.info("Params updated: %s", ", ".join(changed))
        broadcast_state(engine.get_state())
    return jsonify({"success": True, "changed": changed})


@app.route("/api/test-connection")
def api_test_connection():
    client = _create_api_client()
    info = client.check_connection()
    if info["connected"]:
        price = client.get_index_price("BTCUSDT")
        info["btc_price"] = price
        try:
            usdt = client.get_account_summary(currency="USDT")
            if usdt:
                info["usdt_balance"] = usdt.get("balance", 0)
        except Exception:
            pass
        try:
            btc = client.get_account_summary(currency="BTC")
            if btc:
                info["btc_balance"] = btc.get("balance", 0)
        except Exception:
            pass
    return jsonify(info)


@app.route("/api/credentials", methods=["GET"])
def api_credentials():
    """返回当前 API 凭证信息（脱敏）"""
    masked = BINANCE_API_KEY[:4] + "****" if len(BINANCE_API_KEY) > 4 else "****"
    return jsonify({"client_id_masked": masked, "testnet": USE_TESTNET})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global engine
    if request.method == "GET":
        if engine is None:
            return jsonify({"error": "Engine not initialized"}), 503
        return jsonify(engine.cfg)
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data"}), 400
    if engine and engine._running:
        return jsonify({"success": False, "message": "Cannot modify config while running"}), 400
    with engine_lock:
        if engine is None:
            engine = _create_engine()
        for key, val in data.items():
            if key in engine.cfg:
                engine.cfg[key] = val
    return jsonify({"success": True, "message": "Config updated"})


@app.route("/api/kline")
def api_kline():
    """拉取 BTC K 线数据（通过币安公共 API，无需鉴权）"""
    import requests as _req, time as _time
    try:
        # K 线数据始终从主网公共 API 拉取（Demo 的模拟数据不会实时更新）
        base = "https://api.binance.com/api"
        end = int(_time.time() * 1000)
        start = end - 3 * 3600 * 1000
        resp = _req.get(f"{base}/v3/klines", params={
            "symbol": "BTCUSDT", "interval": "5m",
            "startTime": start, "endTime": end, "limit": 150,
        }, timeout=10)
        klines = resp.json()
        if not isinstance(klines, list):
            return jsonify({"error": "no data"}), 500
        opens, closes, highs, lows, volumes, ticks = [], [], [], [], [], []
        for k in klines:
            opens.append(float(k[1]))
            closes.append(float(k[4]))
            highs.append(float(k[2]))
            lows.append(float(k[3]))
            volumes.append(float(k[5]))
            ticks.append(int(k[0]))
        return jsonify({
            "status": "ok", "open": opens, "close": closes,
            "high": highs, "low": lows, "volume": volumes, "ticks": ticks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("  BTC 收益增强策略 (币安版) - Dashboard + WebSocket")
    print("  http://localhost:5052")
    print(f"  交易所: {EXCHANGE.upper()}  测试网: {USE_TESTNET}")
    print("=" * 60)
    app.run(host="127.0.0.1", port=BINANCE_PORT, debug=False)

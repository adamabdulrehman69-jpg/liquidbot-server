import time
import random
import requests
import os
import json
import threading
import hmac
import hashlib
import uuid
import math
from datetime import datetime, timezone
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---- KEEP-ALIVE WEB SERVER ----
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        status = {
            "status": "running",
            "scan": scan_counter,
            "mode": "LIVE" if LIVE_TRADING else "PAPER",
            "time": datetime.now(timezone.utc).isoformat()
        }
        self.wfile.write(json.dumps(status).encode())
    def log_message(self, format, *args):
        pass

def start_keep_alive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), KeepAliveHandler)
    log(f"   Keep-alive server on port {port}")
    server.serve_forever()

# ---- CONFIG ----
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jcwvfgiudhzdpmqibwji.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_dE-1zCOEwJzWHA3ujLyCdw_UPFvdKf5")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
COINBASE_API_KEY = os.environ.get("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.environ.get("COINBASE_API_SECRET", "")
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"  # flip to true when ready

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "180"))
START_CAD = 100.0
TRADING_FEE = 0.006        # Coinbase Advanced taker fee 0.6% (market orders)
MAKER_FEE = 0.004          # Coinbase Advanced maker fee 0.4% (limit orders that wait)
SLIPPAGE = 0.001           # 0.1% slippage for real orders
LEARN_EVERY = 5
WEEKLY_REPORT_SCANS = 336
MAX_DAILY_LOSS_CAD = 10.0
STOP_LOSS_PCT = 0.02        # sell if down 2%
TAKE_PROFIT_PCT = 0.03      # at +3% the trailing stop turns on (no fixed sell point)
TRAIL_GAP_PCT = 0.012       # trailing stop sits 1.2% under the highest price seen
LIVE_SIZE_PCT = {"medium": 10, "high": 20}   # % of free USDC per trade by confidence (low = skip)
LIMIT_BUY_WAIT = 60         # seconds to wait for a limit buy to fill before cancelling
HARD_STOP_BALANCE = 35.0    # stop if balance drops below $35 (50% of starting USDC)
MARKETS_PER_SCAN = 3
FULL_SCAN_EVERY = int(os.environ.get("FULL_SCAN_EVERY", "4"))   # look for new buys every 4th loop (~12 min); positions checked every loop
MIN_PREFILTER_SIGNALS = 2   # coin needs this many free bullish signals before we pay to ask Claude
MAX_CLAUDE_CALLS = 3        # max coins sent to Claude per full scan
FORCE_CLOSE_ON_START = os.environ.get("FORCE_CLOSE_ON_START", "false").lower() == "true"  # sells all open positions once per boot
_force_closed_users = set()
INSIGHT_KEY = "live" if LIVE_TRADING else "main"   # live learning never mixes with paper learning
open_trade_meta = {}   # (user_id, market) -> confidence/reason from when the position opened

# Top markets with good Hyperliquid data
MARKETS = [
    "BTC", "ETH", "SOL", "AVAX", "LINK", "ARB", "BNB", "XRP",
    "DOGE", "ADA", "LTC", "NEAR", "APT", "OP",
    "INJ", "SUI", "TIA", "WIF", "JUP", "HYPE"
]

# Coinbase market pairs — USDC pairs
COINBASE_PAIRS = {
    "BTC": "BTC-USDC", "ETH": "ETH-USDC", "SOL": "SOL-USDC",
    "AVAX": "AVAX-USDC", "LINK": "LINK-USDC", "DOGE": "DOGE-USDC",
    "ADA": "ADA-USDC", "LTC": "LTC-USDC", "XRP": "XRP-USDC",
    "BNB": "BNB-USDC"
}

user_learning = {}
user_daily_pnl = {}
user_daily_reset = {}
scan_counter = 0

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ---- COINBASE API (Official SDK) ----
def get_coinbase_client():
    """Get Coinbase Advanced Trade client using official SDK"""
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return None
    try:
        from coinbase.rest import RESTClient
        # Coinbase SDK expects the key with proper \n newlines
        secret = COINBASE_API_SECRET.strip().replace("\\n", "\n")
        # If no PEM headers, add them
        if "-----BEGIN" not in secret:
            secret = f"-----BEGIN EC PRIVATE KEY-----\n{secret}\n-----END EC PRIVATE KEY-----\n"
        return RESTClient(api_key=COINBASE_API_KEY, api_secret=secret)
    except ImportError:
        log("  ⚠️ coinbase-advanced-py not installed")
        return None
    except Exception as e:
        log(f"  Coinbase client error: {e}")
        return None

def coinbase_request(method, path, body=None):
    """Make authenticated request using official Coinbase SDK"""
    client = get_coinbase_client()
    if not client:
        return None
    try:
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, data=json.dumps(body) if body else None)
        return resp if isinstance(resp, dict) else resp.__dict__ if hasattr(resp, '__dict__') else None
    except Exception as e:
        log(f"  Coinbase API error: {e}")
        return None

def get_coinbase_balance():
    """Get real CAD balance from Coinbase"""
    try:
        client = get_coinbase_client()
        if not client:
            return None
        accounts = client.get_accounts()
        acct_list = accounts.accounts if hasattr(accounts, 'accounts') else accounts.get('accounts', [])
        # Try USDC first, then USD, then CAD
        for currency in ["USDC", "USD", "CAD"]:
            for acc in acct_list:
                curr = acc.currency if hasattr(acc, 'currency') else acc.get('currency', '')
                if curr == currency:
                    bal_obj = acc.available_balance if hasattr(acc, 'available_balance') else acc.get('available_balance', {})
                    val = bal_obj.value if hasattr(bal_obj, 'value') else bal_obj.get('value', 0)
                    bal = float(val)
                    if bal > 0:
                        log(f"  💵 Real Coinbase balance: ${bal:.2f} {currency}")
                        return bal
        log("  ⚠️ No CAD or USD balance found")
        return None
    except Exception as e:
        log(f"  Coinbase balance error: {e}")
        return None

def place_coinbase_order(product_id, side, size_cad):
    """Place a real market order on Coinbase Advanced Trade using official SDK"""
    if not COINBASE_API_KEY:
        return None
    try:
        import uuid
        client = get_coinbase_client()
        if not client:
            return None

        # Check real balance before placing order
        real_balance = get_coinbase_balance()
        if real_balance is not None and size_cad > real_balance * 0.95:
            log(f"  ⚠️ Order size ${size_cad:.2f} too large for balance ${real_balance:.2f} — adjusting")
            size_cad = real_balance * 0.90

        if size_cad < 2.0:
            log(f"  ⚠️ Order size too small (${size_cad:.2f}) — skipping")
            return None

        order_id = str(uuid.uuid4())
        log(f"  Placing order: {product_id} BUY ${size_cad:.2f} USDC")
        try:
            # Try market_order_buy specifically for buying with quote currency
            order = client.market_order_buy(
                client_order_id=order_id,
                product_id=product_id,
                quote_size=str(round(size_cad, 2))
            )
        except AttributeError:
            # Fallback to market_order if market_order_buy not available
            order = client.market_order(
                client_order_id=order_id,
                product_id=product_id,
                side="BUY",
                quote_size=str(round(size_cad, 2))
            )

        # Real success check — no more "assume success"
        if not _cb_get(order, "success", False):
            err = _cb_get(order, "error_response", {})
            reason = f"{_cb_get(err, 'error', '')} {_cb_get(err, 'message', '')} {_cb_get(err, 'preview_failure_reason', '')}".strip()
            log(f"  ❌ BUY rejected: {product_id} — {reason or order}")
            return None

        order_id_resp = _cb_get(_cb_get(order, "success_response", {}), "order_id") or _cb_get(order, "order_id", "?")

        # Real fill: exact coins received + real entry price
        fill = {}
        for _ in range(3):
            try:
                o = _cb_get(client.get_order(order_id_resp), "order", {})
                if _cb_get(o, "status") == "FILLED":
                    fill = {
                        "avg_price": float(_cb_get(o, "average_filled_price", 0) or 0),
                        "filled_size": float(_cb_get(o, "filled_size", 0) or 0),
                        "spent": float(_cb_get(o, "total_value_after_fees", 0) or 0) or float(size_cad),
                    }
                    break
            except Exception as e:
                log(f"  Fill lookup error: {e}")
            time.sleep(1)

        log(f"  ✅ Real order placed: {product_id} BUY ${size_cad:.2f} USDC — ID: {str(order_id_resp)[:8]}"
            + (f" | got {fill['filled_size']} @ ${fill['avg_price']:.4f}" if fill else " | fill not confirmed yet"))
        return {"order_id": order_id_resp, "size_usdc": size_cad, **fill}
    except Exception as e:
        log(f"  Order error: {e}")
        return None

def place_limit_buy(product_id, size_usdc):
    """
    Post-only limit BUY at the best bid, so we pay the 0.4% maker fee instead of 0.6%.
    Waits LIMIT_BUY_WAIT seconds, cancels whatever didn't fill. Doesn't chase the price.
    """
    client = get_coinbase_client()
    if not client:
        return None
    try:
        real_balance = get_coinbase_balance()
        if real_balance is not None and size_usdc > real_balance * 0.95:
            size_usdc = real_balance * 0.90
        if size_usdc < 2.0:
            log(f"  ⚠️ Order size too small (${size_usdc:.2f}) — skipping")
            return None

        p = client.get_product(product_id)
        base_inc = str(_cb_get(p, "base_increment", "0.00000001"))
        quote_inc = str(_cb_get(p, "quote_increment", "0.01"))
        base_min = float(_cb_get(p, "base_min_size", 0) or 0)

        book = client.get_best_bid_ask(product_ids=[product_id])
        pb = (_cb_get(book, "pricebooks", []) or [None])[0]
        bid = float(_cb_get((_cb_get(pb, "bids", []) or [{}])[0], "price", 0) or 0)
        if bid <= 0:
            log(f"  ⚠️ No bid price for {product_id} — skipping")
            return None

        limit_price = floor_to_increment(bid, quote_inc)
        base_size = floor_to_increment(size_usdc / (1 + MAKER_FEE) / float(limit_price), base_inc)
        if float(base_size) < base_min:
            log(f"  ⚠️ {product_id} size {base_size} below min {base_min} — skipping")
            return None

        log(f"  Placing LIMIT BUY: {product_id} {base_size} @ ${limit_price} (~${size_usdc:.2f} USDC, maker fee)")
        order = client.limit_order_gtc_buy(
            client_order_id=f"lqb-{uuid.uuid4()}",
            product_id=product_id,
            base_size=base_size,
            limit_price=limit_price,
            post_only=True
        )
        if not _cb_get(order, "success", False):
            err = _cb_get(order, "error_response", {})
            reason = f"{_cb_get(err, 'error', '')} {_cb_get(err, 'message', '')} {_cb_get(err, 'preview_failure_reason', '')}".strip()
            log(f"  ❌ LIMIT BUY rejected: {product_id} — {reason or order}")
            return None
        order_id = _cb_get(_cb_get(order, "success_response", {}), "order_id") or _cb_get(order, "order_id")

        # Wait for fill
        o = {}
        waited = 0
        while waited < LIMIT_BUY_WAIT:
            time.sleep(5); waited += 5
            try:
                o = _cb_get(client.get_order(order_id), "order", {})
                if _cb_get(o, "status") == "FILLED":
                    break
            except Exception as e:
                log(f"  Fill check error: {e}")

        if _cb_get(o, "status") != "FILLED":
            try:
                client.cancel_orders(order_ids=[order_id])
            except Exception as e:
                log(f"  Cancel error: {e}")
            time.sleep(1)
            try:
                o = _cb_get(client.get_order(order_id), "order", {})
            except Exception:
                pass

        filled = float(_cb_get(o, "filled_size", 0) or 0)
        if filled <= 0:
            log(f"  ⏭️ LIMIT BUY didn't fill in {LIMIT_BUY_WAIT}s — cancelled, no trade")
            return None

        avg = float(_cb_get(o, "average_filled_price", 0) or 0) or float(limit_price)
        spent = float(_cb_get(o, "total_value_after_fees", 0) or 0) or filled * avg * (1 + MAKER_FEE)
        log(f"  ✅ BOUGHT {filled} {product_id.split('-')[0]} @ ${avg:.4f} (${spent:.2f} USDC) — ID: {str(order_id)[:8]}")
        return {"order_id": order_id, "filled_size": filled, "avg_price": avg, "spent": spent}
    except Exception as e:
        log(f"  Limit buy error: {e}")
        return None

def get_coinbase_order(order_id):
    """Check status of a placed order"""
    return coinbase_request("GET", f"/api/v3/brokerage/orders/historical/{order_id}")

# ---- SELL SYSTEM (base_size) ----
_product_cache = {}

def _cb_get(obj, key, default=None):
    """Read a field from an SDK response object or a dict"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, None)
    if val is None:
        try:
            val = obj[key]
        except Exception:
            val = None
    return default if val is None else val

def get_product_rules(client, product_id):
    """base_increment + base_min_size for a pair (cached)"""
    if product_id not in _product_cache:
        p = client.get_product(product_id)
        _product_cache[product_id] = {
            "base_increment": str(_cb_get(p, "base_increment", "0.00000001")),
            "base_min_size": float(_cb_get(p, "base_min_size", 0) or 0),
        }
    return _product_cache[product_id]

def get_coin_balance(client, currency):
    """Real available coin balance on Coinbase (e.g. 'LINK' -> 0.3921)"""
    cursor = None
    while True:
        resp = client.get_accounts(limit=250, cursor=cursor) if cursor else client.get_accounts(limit=250)
        for acc in _cb_get(resp, "accounts", []) or []:
            if _cb_get(acc, "currency") == currency:
                bal = _cb_get(acc, "available_balance", {})
                return float(_cb_get(bal, "value", 0) or 0)
        if not _cb_get(resp, "has_next", False):
            return 0.0
        cursor = _cb_get(resp, "cursor")

def floor_to_increment(amount, increment):
    """Round DOWN to the pair's step size, returned as a string Coinbase accepts"""
    inc = float(increment)
    decimals = len((increment.split(".")[1] if "." in increment else "").rstrip("0"))
    steps = math.floor(amount / inc + 1e-9)
    return f"{steps * inc:.{decimals}f}"

def sell_coinbase_position(product_id, max_coin=None):
    """
    Market SELL using base_size (coin amount), never quote_size.
    Uses the real Coinbase balance so fees/rounding can't cause INSUFFICIENT_FUND.
    Returns dict: {"status": "sold"|"nothing_to_sell"|"failed", ...}
    """
    client = get_coinbase_client()
    if not client:
        return {"status": "failed", "error": "no Coinbase client"}
    try:
        base_currency = product_id.split("-")[0]
        rules = get_product_rules(client, product_id)
        balance = get_coin_balance(client, base_currency)

        # Only sell what this position holds (never touch coins held outside the bot)
        amount = min(balance, max_coin) if max_coin else balance
        base_size = floor_to_increment(amount, rules["base_increment"])

        if float(base_size) <= 0 or float(base_size) < rules["base_min_size"]:
            log(f"  ⚠️ {product_id}: sellable {base_size} {base_currency} below min {rules['base_min_size']} (balance {balance})")
            return {"status": "nothing_to_sell", "balance": balance}

        log(f"  Placing SELL: {product_id} {base_size} {base_currency} (balance {balance})")
        order = client.market_order_sell(
            client_order_id=f"lqb-{uuid.uuid4()}",
            product_id=product_id,
            base_size=base_size
        )

        if not _cb_get(order, "success", False):
            err = _cb_get(order, "error_response", {})
            reason = f"{_cb_get(err, 'error', '')} {_cb_get(err, 'message', '')} {_cb_get(err, 'preview_failure_reason', '')}".strip()
            log(f"  ❌ SELL rejected: {product_id} — {reason or order}")
            return {"status": "failed", "error": reason or str(order)}

        order_id = _cb_get(_cb_get(order, "success_response", {}), "order_id") or _cb_get(order, "order_id")

        # Get real fill numbers (IOC fills almost instantly, retry briefly)
        fill = {}
        for _ in range(3):
            try:
                o = _cb_get(client.get_order(order_id), "order", {})
                if _cb_get(o, "status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                    fill = {
                        "avg_price": float(_cb_get(o, "average_filled_price", 0) or 0),
                        "filled_size": float(_cb_get(o, "filled_size", 0) or 0),
                        "filled_value": float(_cb_get(o, "filled_value", 0) or 0),
                        "fees": float(_cb_get(o, "total_fees", 0) or 0),
                    }
                    break
            except Exception as e:
                log(f"  Fill lookup error: {e}")
            time.sleep(1)

        log(f"  ✅ SOLD {product_id} {base_size} {base_currency} @ ${fill.get('avg_price', 0):.4f} — ID: {str(order_id)[:8]}")
        return {"status": "sold", "order_id": order_id, "base_size": base_size, **fill}
    except Exception as e:
        log(f"  ❌ SELL error: {product_id} — {e}")
        return {"status": "failed", "error": str(e)}

def close_coinbase_position(product_id, side, size_usd=None, max_coin=None):
    """Close a long spot position (sell by coin amount)"""
    return sell_coinbase_position(product_id, max_coin=max_coin)

# ---- DAILY LOSS TRACKING ----
def check_daily_loss(user_id, pnl):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if user_daily_reset.get(user_id) != today:
        user_daily_pnl[user_id] = 0
        user_daily_reset[user_id] = today
        log(f"  📅 New day — daily P&L reset")
    user_daily_pnl[user_id] = user_daily_pnl.get(user_id, 0) + pnl
    if user_daily_pnl[user_id] <= -MAX_DAILY_LOSS_CAD:
        log(f"  🛑 MAX DAILY LOSS — ${abs(user_daily_pnl[user_id]):.2f} CAD lost today")
        return True
    return False

# ---- WEEKLY REPORT ----
def send_weekly_report(user_id):
    try:
        trades = supa_get("trades", f"user_id=eq.{user_id}&order=created_at.desc&limit=200&select=market,side,pnl_cad,confidence,created_at")
        if not trades or len(trades) < 5:
            return
        week_ago = datetime.now(timezone.utc).timestamp() - 7 * 24 * 60 * 60
        week_trades = [t for t in trades if datetime.fromisoformat(t['created_at'].replace('Z','+00:00')).timestamp() > week_ago]
        if not week_trades:
            return
        total = len(week_trades)
        wins = sum(1 for t in week_trades if float(t.get('pnl_cad', 0)) > 0)
        win_rate = round(wins / total * 100, 1)
        total_pnl = sum(float(t.get('pnl_cad', 0)) for t in week_trades)
        market_pnl = defaultdict(float)
        for t in week_trades:
            market_pnl[t['market']] += float(t.get('pnl_cad', 0))
        best_market = max(market_pnl, key=market_pnl.get) if market_pnl else "N/A"
        emoji = "📈" if total_pnl >= 0 else "📉"
        supa_post("alerts", {
            "user_id": user_id, "type": "win" if total_pnl >= 0 else "loss",
            "title": f"{emoji} Weekly Performance Report",
            "description": f"{total} trades | Win rate: {win_rate}% | P&L: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} CAD | Best: {best_market}"
        })
        log(f"  📊 Weekly report: {win_rate}% wr | {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} CAD")
    except Exception as e:
        log(f"  Weekly report error: {e}")

# ---- SUPABASE ----
def supa_get(table, filters=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{filters}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"})
    return r.json() if r.ok else []

def supa_post(table, data):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data,
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"})
    return r.ok

def save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
        json={"balance_cad": round(balance, 4), "total_pnl_cad": round(total_pnl, 4),
              "trade_count": trade_count, "trade_size_pct": trade_size_pct,
              "leverage": leverage, "risk": risk,
              "updated_at": datetime.now(timezone.utc).isoformat()},
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"})
    if r.ok:
        log(f"  Saved — balance: ${balance:.2f} {'USDC' if LIVE_TRADING else 'CAD'}")
    else:
        log(f"  Save failed: {r.status_code}")

def get_all_users():
    rows = supa_get("bot_settings", "select=user_id,balance_cad,total_pnl_cad,trade_count,trade_size_pct,leverage,risk,bot_enabled")
    return rows if isinstance(rows, list) else []

# ---- LIVE PRICES ----
def fetch_prices():
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
            json={"type": "allMids"},
            headers={"Content-Type": "application/json"}, timeout=10)
        return r.json() if r.ok else {}
    except Exception as e:
        log(f"Price fetch error: {e}")
        return {}

# ---- FEAR & GREED INDEX ----
fear_greed_cache = {"value": None, "label": None, "fetched_at": 0}

def fetch_fear_greed():
    """Fetch crypto fear & greed index — cached for 1 hour"""
    global fear_greed_cache
    now = time.time()
    # Use cache if less than 1 hour old
    if fear_greed_cache["value"] and now - fear_greed_cache["fetched_at"] < 3600:
        return fear_greed_cache
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r.ok:
            data = r.json()["data"][0]
            value = int(data["value"])
            label = data["value_classification"]
            fear_greed_cache = {"value": value, "label": label, "fetched_at": now}
            log(f"  📊 Fear & Greed: {value} ({label})")
            return fear_greed_cache
    except Exception as e:
        log(f"  Fear & greed error: {e}")
    return fear_greed_cache

# ---- MARKET DATA ----
def fetch_market_data(market):
    try:
        now_ms = int(time.time() * 1000)
        r = requests.post("https://api.hyperliquid.xyz/info",
            json={"type": "candleSnapshot", "req": {
                "coin": market, "interval": "1h",
                "startTime": now_ms - 24 * 60 * 60 * 1000, "endTime": now_ms}},
            headers={"Content-Type": "application/json"}, timeout=10)
        if not r.ok:
            return None
        candles = r.json()
        if not candles or len(candles) < 3:
            return None
        closes = [float(c["c"]) for c in candles]
        highs = [float(c["h"]) for c in candles]
        lows = [float(c["l"]) for c in candles]
        volumes = [float(c["v"]) for c in candles]
        current = closes[-1]
        change_24h = ((current - closes[0]) / closes[0]) * 100
        momentum_pct = ((closes[-1] - closes[-4]) / closes[-4]) * 100 if len(closes) >= 4 else 0
        sma5 = sum(closes[-5:]) / min(5, len(closes))
        sma10 = sum(closes[-10:]) / min(10, len(closes))
        avg_vol = sum(volumes[:-1]) / max(1, len(volumes) - 1)
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
        gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
        losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
        avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else sum(gains) / max(1, len(gains))
        avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else sum(losses) / max(1, len(losses))
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))
        high_24h = max(highs)
        low_24h = min(lows)
        price_position = ((current - low_24h) / (high_24h - low_24h)) * 100 if high_24h != low_24h else 50
        return {
            "current_price": current, "change_24h_pct": round(change_24h, 2),
            "momentum_3h_pct": round(momentum_pct, 2),
            "above_sma5": current > sma5, "above_sma10": current > sma10,
            "rsi": round(rsi, 1), "volume_ratio": round(vol_ratio, 2),
            "high_24h": high_24h, "low_24h": low_24h,
            "price_position_pct": round(price_position, 1),
        }
    except Exception as e:
        log(f"  Market data error {market}: {e}")
        return None

# ---- FUNDING RATE ----
def fetch_funding_rate(market):
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            headers={"Content-Type": "application/json"}, timeout=10)
        if not r.ok:
            return None
        data = r.json()
        if len(data) < 2:
            return None
        coins = [a["name"] for a in data[0].get("universe", [])]
        if market not in coins:
            return None
        ctx = data[1][coins.index(market)]
        funding = float(ctx.get("funding", 0))
        return {
            "funding_rate": round(funding * 100, 4),
            "funding_signal": "BEARISH" if funding > 0.0001 else "BULLISH" if funding < -0.0001 else "NEUTRAL"
        }
    except:
        return None

# ---- ORDER BOOK ----
def fetch_orderbook(market):
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
            json={"type": "l2Book", "coin": market},
            headers={"Content-Type": "application/json"}, timeout=10)
        if not r.ok:
            return None
        levels = r.json().get("levels", [[], []])
        bids = levels[0][:10]
        asks = levels[1][:10]
        bid_vol = sum(float(b["sz"]) for b in bids)
        ask_vol = sum(float(a["sz"]) for a in asks)
        total = bid_vol + ask_vol
        ratio = round(bid_vol / ask_vol, 2) if ask_vol > 0 else 1
        return {
            "bid_pct": round(bid_vol / total * 100, 1) if total > 0 else 50,
            "ask_pct": round(ask_vol / total * 100, 1) if total > 0 else 50,
            "bid_ask_ratio": ratio,
            "orderbook_signal": "BULLISH" if ratio > 1.2 else "BEARISH" if ratio < 0.8 else "BALANCED"
        }
    except:
        return None

# ---- IMPROVED PERSISTENT SELF LEARNING ----
def get_trade_history(user_id, limit=100):
    """Trades for learning. Live mode ignores paper trades completely."""
    fetch = limit * 5 if LIVE_TRADING else limit
    rows = supa_get("trades", f"user_id=eq.{user_id}&order=created_at.desc&limit={fetch}&select=market,side,pnl_cad,confidence,reason,created_at")
    rows = rows if isinstance(rows, list) else []
    if LIVE_TRADING:
        rows = [r for r in rows if str(r.get("reason") or "").startswith("[LIVE]")][:limit]
    return rows

def get_live_pnl_today(user_id):
    """Real P&L from today's closed live trades (survives restarts)"""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    rows = supa_get("trades", f"user_id=eq.{user_id}&created_at=gte.{today}T00:00:00Z&select=pnl_cad,reason")
    rows = rows if isinstance(rows, list) else []
    return sum(float(r.get("pnl_cad") or 0) for r in rows if str(r.get("reason") or "").startswith("[LIVE]"))

def get_live_account_value(user_id, prices):
    """Real USDC on Coinbase + current value of open positions"""
    usdc = get_coinbase_balance()
    if usdc is None:
        return None, None
    held = 0.0
    for pos in get_open_positions(user_id):
        price = float(prices.get(pos.get("market"), 0) or 0)
        held += float(pos.get("amount_coin", 0) or 0) * price
    return usdc, usdc + held

def save_insights(user_id, insights):
    """Save learning insights to Supabase so they persist across restarts"""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/learning_insights",
            json={
                "user_id": user_id,
                "insight_key": INSIGHT_KEY,
                "insight_data": insights,
                "updated_at": datetime.now(timezone.utc).isoformat()
            },
            headers={
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal"
            }
        )
        return r.ok
    except Exception as e:
        log(f"  Save insights error: {e}")
        return False

def load_insights(user_id):
    """Load previously saved insights from Supabase"""
    try:
        rows = supa_get("learning_insights", f"user_id=eq.{user_id}&insight_key=eq.{INSIGHT_KEY}&select=insight_data")
        if rows and len(rows) > 0:
            return rows[0].get("insight_data", {})
        return {}
    except:
        return {}

def analyze_trades(trades):
    """Deep analysis of trade history for persistent learning"""
    if len(trades) < 5:
        return None

    market_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    confidence_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    side_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    hour_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    signal_patterns = defaultdict(lambda: {"wins": 0, "losses": 0})
    winning_reasons = []
    losing_reasons = []

    for t in trades:
        market = t.get("market", "?")
        pnl = float(t.get("pnl_cad", 0))
        confidence = t.get("confidence", "?")
        side = t.get("side", "?")
        reason = t.get("reason", "")
        won = pnl > 0

        # Track by market
        market_stats[market]["wins" if won else "losses"] += 1
        market_stats[market]["pnl"] += pnl

        # Track by confidence
        confidence_stats[confidence]["wins" if won else "losses"] += 1

        # Track by side (long vs short)
        side_stats[side]["wins" if won else "losses"] += 1

        # Track by hour of day
        try:
            hour = datetime.fromisoformat(t.get("created_at","").replace("Z","+00:00")).hour
            hour_stats[hour]["wins" if won else "losses"] += 1
        except:
            pass

        # Extract signal patterns from reason
        reason_lower = reason.lower()
        if "rsi" in reason_lower and "oversold" in reason_lower:
            signal_patterns["rsi_oversold"]["wins" if won else "losses"] += 1
        if "rsi" in reason_lower and "overbought" in reason_lower:
            signal_patterns["rsi_overbought"]["wins" if won else "losses"] += 1
        if "volume" in reason_lower and ("high" in reason_lower or "spike" in reason_lower):
            signal_patterns["high_volume"]["wins" if won else "losses"] += 1
        if "uptrend" in reason_lower:
            signal_patterns["uptrend"]["wins" if won else "losses"] += 1
        if "downtrend" in reason_lower:
            signal_patterns["downtrend"]["wins" if won else "losses"] += 1
        if "funding" in reason_lower and "bullish" in reason_lower:
            signal_patterns["bullish_funding"]["wins" if won else "losses"] += 1
        if "orderbook" in reason_lower or "order book" in reason_lower:
            if "bullish" in reason_lower:
                signal_patterns["bullish_ob"]["wins" if won else "losses"] += 1

        if won and reason:
            winning_reasons.append(reason[:100])
        elif not won and reason:
            losing_reasons.append(reason[:100])

    # Calculate win rates
    total_wins = sum(1 for t in trades if float(t.get("pnl_cad", 0)) > 0)
    win_rate = round(total_wins / len(trades) * 100, 1)
    total_pnl = sum(float(t.get("pnl_cad", 0)) for t in trades)

    market_wr = {}
    for m, s in market_stats.items():
        total = s["wins"] + s["losses"]
        if total >= 3:  # need at least 3 trades to be meaningful
            market_wr[m] = {"wr": round(s["wins"]/total*100,1), "trades": total, "pnl": round(s["pnl"],2)}

    # Best hours (top 3 by win rate with at least 3 trades)
    hour_wr = {}
    for h, s in hour_stats.items():
        total = s["wins"] + s["losses"]
        if total >= 3:
            hour_wr[h] = round(s["wins"]/total*100, 1)
    best_hours = sorted(hour_wr.keys(), key=lambda h: hour_wr[h], reverse=True)[:3]

    # Signal pattern win rates
    pattern_wr = {}
    for p, s in signal_patterns.items():
        total = s["wins"] + s["losses"]
        if total >= 2:
            pattern_wr[p] = round(s["wins"]/total*100, 1)

    # Best/worst side
    long_wr = 0
    if side_stats["long"]["wins"] + side_stats["long"]["losses"] > 0:
        long_wr = round(side_stats["long"]["wins"] / (side_stats["long"]["wins"] + side_stats["long"]["losses"]) * 100, 1)
    short_wr = 0
    if side_stats["short"]["wins"] + side_stats["short"]["losses"] > 0:
        short_wr = round(side_stats["short"]["wins"] / (side_stats["short"]["wins"] + side_stats["short"]["losses"]) * 100, 1)

    best_markets = sorted(market_wr.keys(), key=lambda m: market_wr[m]["pnl"], reverse=True)[:4]
    worst_markets = sorted(market_wr.keys(), key=lambda m: market_wr[m]["pnl"])[:3]

    return {
        "total_trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "best_markets": best_markets,
        "worst_markets": worst_markets,
        "market_win_rates": market_wr,
        "best_hours": best_hours,
        "hour_win_rates": hour_wr,
        "pattern_win_rates": pattern_wr,
        "long_win_rate": long_wr,
        "short_win_rate": short_wr,
        "confidence_stats": dict(confidence_stats),
        "winning_reasons": winning_reasons[-5:],
        "losing_reasons": losing_reasons[-3:],
    }

def build_learning_prompt(analysis, saved_insights=None):
    """Build a rich learning context for Claude using both current and historical insights"""
    if not analysis and not saved_insights:
        return ""

    lines = []

    # Merge current analysis with saved historical insights
    if saved_insights and analysis:
        # Weight recent trades more — blend current win rate with historical
        hist_wr = saved_insights.get("win_rate", analysis["win_rate"])
        blended_wr = round(0.6 * analysis["win_rate"] + 0.4 * hist_wr, 1)
        lines.append(f"\n🧠 SELF-LEARNING INSIGHTS (current: {analysis['win_rate']}% wr | historical: {hist_wr}% wr | blended: {blended_wr}%):")
    elif analysis:
        lines.append(f"\n🧠 SELF-LEARNING INSIGHTS ({analysis['total_trades']} trades | {analysis['win_rate']}% win rate | P&L: {'+' if analysis['total_pnl']>=0 else ''}${analysis['total_pnl']} CAD):")

    a = analysis or saved_insights or {}

    # Market preferences
    mwr = a.get("market_win_rates", {})
    good_markets = [m for m, d in mwr.items() if (d["wr"] if isinstance(d, dict) else d) >= 58]
    bad_markets = [m for m, d in mwr.items() if (d["wr"] if isinstance(d, dict) else d) < 40]
    if good_markets:
        lines.append(f"- PREFER markets: {', '.join(good_markets)} (strong win rate)")
    if bad_markets:
        lines.append(f"- AVOID markets: {', '.join(bad_markets)} (losing consistently)")

    # Side preference
    long_wr = a.get("long_win_rate", 50)
    short_wr = a.get("short_win_rate", 50)
    if abs(long_wr - short_wr) > 10:
        better = "LONG" if long_wr > short_wr else "SHORT"
        lines.append(f"- {better} trades perform better ({long_wr}% long wr vs {short_wr}% short wr) — bias toward {better}")

    # Best trading hours
    best_hours = a.get("best_hours", [])
    if best_hours:
        lines.append(f"- Best trading hours (UTC): {', '.join([f'{h}:00' for h in best_hours[:3]])}")

    # Signal patterns that work
    pattern_wr = a.get("pattern_win_rates", {})
    strong_patterns = [p for p, wr in pattern_wr.items() if wr >= 60]
    weak_patterns = [p for p, wr in pattern_wr.items() if wr < 40]
    if strong_patterns:
        lines.append(f"- STRONG signals historically: {', '.join(strong_patterns)}")
    if weak_patterns:
        lines.append(f"- WEAK signals to discount: {', '.join(weak_patterns)}")

    # Confidence level performance
    cs = a.get("confidence_stats", {})
    for conf_level in ["high", "medium", "low"]:
        if conf_level in cs:
            c = cs[conf_level]
            total = c["wins"] + c["losses"]
            if total >= 3:
                wr = round(c["wins"]/total*100)
                if conf_level == "low" and wr < 45:
                    lines.append(f"- SKIP low confidence trades — only {wr}% win rate")
                elif conf_level == "high" and wr >= 60:
                    lines.append(f"- HIGH confidence trades working well — {wr}% win rate, increase size")

    # Winning patterns
    winning_reasons = a.get("winning_reasons", [])
    if winning_reasons:
        lines.append(f"- Recent winning setups: {winning_reasons[-1][:80]}")

    return "\n".join(lines)

# ---- CLAUDE AI ----
def call_claude(market, price_str, risk, leverage, market_data, learning_context="", funding=None, orderbook=None):
    if not ANTHROPIC_KEY:
        if market_data:
            rsi = market_data["rsi"]
            vol = market_data["volume_ratio"]
            signals = 0
            side = "long"
            if rsi < 35: signals += 1; side = "long"
            elif rsi > 65: signals += 1; side = "short"
            if vol > 1.0: signals += 1
            if market_data["above_sma5"] and market_data["above_sma10"]: signals += 0.5; side = "long"
            elif not market_data["above_sma5"] and not market_data["above_sma10"]: signals += 0.5; side = "short"
            if orderbook and orderbook["bid_ask_ratio"] > 1.2: signals += 0.5; side = "long"
            elif orderbook and orderbook["bid_ask_ratio"] < 0.8: signals += 0.5; side = "short"
            conf = "high" if signals >= 2.5 else "medium" if signals >= 1.5 else "low"
            return {"trade": signals >= 1.5, "side": side, "confidence": conf,
                    "reason": f"Fallback: RSI={rsi}, vol={vol:.1f}x, signals={signals:.1f}"}
        return {"trade": False, "side": "long", "confidence": "low", "reason": "No data"}

    try:
        if market_data:
            md = market_data
            trend = "UPTREND" if md["above_sma5"] and md["above_sma10"] else \
                    "DOWNTREND" if not md["above_sma5"] and not md["above_sma10"] else "SIDEWAYS"
            rsi_signal = "OVERSOLD — strong LONG" if md["rsi"] < 30 else \
                         "Approaching oversold — consider LONG" if md["rsi"] < 40 else \
                         "Approaching overbought — consider SHORT" if md["rsi"] > 60 else \
                         "OVERBOUGHT — strong SHORT" if md["rsi"] > 70 else "NEUTRAL"
            vol_signal = "HIGH — strong move likely" if md["volume_ratio"] > 1.2 else \
                         "MODERATE" if md["volume_ratio"] > 0.8 else "LOW"
            data_str = f"""
TECHNICAL DATA for {market}:
- Price: {price_str} | 24h: {md['change_24h_pct']}% | 3h momentum: {md['momentum_3h_pct']}%
- Trend: {trend} | RSI: {md['rsi']} — {rsi_signal}
- Volume: {md['volume_ratio']}x — {vol_signal}
- Price in 24h range: {md['price_position_pct']}%"""
            if funding:
                data_str += f"\n- Funding: {funding['funding_rate']}% ({funding['funding_signal']})"
            if orderbook:
                data_str += f"\n- Order book ratio: {orderbook['bid_ask_ratio']} ({orderbook['orderbook_signal']})"
        else:
            data_str = f"Price: {price_str}"

        threshold = "1 strong signal or 2 moderate" if risk == "High" else \
                    "1.5 confirming signals" if risk == "Medium" else "2+ signals"

        mode_note = "LIVE TRADING on Coinbase SPOT — real money. You can ONLY go LONG (buy). Never suggest short. Find oversold/dip buying opportunities." if LIVE_TRADING else \
                    "Paper trading mode — simulate realistic trades."

        cost_note = """
COSTS: each round trip costs about 1% in fees. Stop loss is -2%, and winners are held with a trailing stop once up 3%.
Only say trade=true if you expect at least a 3% move up soon. Small or unclear setups lose money after fees — skip them.

CONFIDENCE (this sets how much money goes in):
- high = 3+ strong bullish signals that agree, INCLUDING volume above 1.0x. Trade size 20%.
- medium = 2 solid bullish signals. Trade size 10%.
- low = anything weaker. Low is never traded, so if it's low just say trade=false.
""" if LIVE_TRADING else ""

        prompt = f"""You are an expert crypto trading bot. {mode_note}

{data_str}
{learning_context}

Risk: {risk} | Threshold: {threshold}

Since we can ONLY BUY on Coinbase spot, look for LONG opportunities:
- RSI <40 = oversold = BUY signal (strong if <30)
- Price at low end of 24h range (<30%) = good entry
- Volume >1.0x = confirms move
- Uptrend (above SMA5 and SMA10) = BUY signal
- Funding negative = bullish = BUY signal
- OB ratio >1.1 = more buyers = BUY signal
- Fear & Greed <40 = fear = contrarian BUY

If conditions are neutral or bearish — say trade=false and wait.
Only trade when you see 1.5+ BULLISH signals.
{cost_note}
JSON only: {{"trade": true/false, "side": "long", "confidence": "low"/"medium"/"high", "reason": "cite bullish signals"}}"""

        r = requests.post("https://api.anthropic.com/v1/messages",
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"}, timeout=15)
        resp = r.json()
        if "error" in resp:
            log(f"  Claude error: {resp['error'].get('message','?')[:60]}")
            return {"trade": False, "side": "long", "confidence": "low", "reason": "API error"}
        text = resp["content"][0]["text"]
        # Robustly extract JSON from response
        text = text.replace("```json","").replace("```","").strip()
        # Find the JSON object within the text
        start = text.find('{')
        end = text.rfind('}') + 1
        if start >= 0 and end > start:
            text = text[start:end]
        # Try to fix common JSON issues
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try extracting individual fields manually
            import re
            trade = "true" in text.lower() and '"trade": true' in text.lower()
            side_match = re.search(r'"side":\s*"(long|short)"', text)
            conf_match = re.search(r'"confidence":\s*"(low|medium|high)"', text)
            reason_match = re.search(r'"reason":\s*"([^"]{0,100})', text)
            result = {
                "trade": trade,
                "side": side_match.group(1) if side_match else "long",
                "confidence": conf_match.group(1) if conf_match else "low",
                "reason": reason_match.group(1) if reason_match else "Partial parse"
            }
        log(f"  Claude: {market} trade={result.get('trade')} {result.get('side')} {result.get('confidence')} | {result.get('reason','')[:60]}")
        return result
    except Exception as e:
        log(f"  Claude error: {e}")
        return {"trade": False, "side": "long", "confidence": "low", "reason": "Error"}

# ---- LIVE POSITION TRACKING ----
def save_live_position(user_id, market, product_id, entry_price, amount_coin, size_usdc):
    """Save an open live position to Supabase"""
    sl_price = round(entry_price * (1 - STOP_LOSS_PCT), 6)
    tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT), 6)
    supa_post("live_positions", {
        "user_id": user_id,
        "market": market,
        "product_id": product_id,
        "side": "long",
        "entry_price": entry_price,
        "amount_coin": amount_coin,
        "size_usdc": size_usdc,
        "stop_loss_price": sl_price,
        "take_profit_price": tp_price,
        "status": "open"
    })
    log(f"  📝 Position saved: {market} entry=${entry_price} SL=${sl_price} TP=${tp_price}")

def get_open_positions(user_id):
    """Get all open live positions"""
    rows = supa_get("live_positions", f"user_id=eq.{user_id}&status=eq.open&select=*")
    return rows if isinstance(rows, list) else []

def close_live_position(position_id, exit_reason):
    """Mark a position as closed"""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/live_positions?id=eq.{position_id}",
        json={"status": "closed"},
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"}
    )
    return r.ok

def recover_trade_meta(user_id, market):
    """After a restart, get the buy's confidence/reason back from its BOUGHT alert"""
    try:
        rows = supa_get("alerts", f"user_id=eq.{user_id}&title=like.*{market}%20BOUGHT&order=created_at.desc&limit=1&select=description")
        desc = rows[0].get("description", "") if rows else ""
        parts = [x.strip() for x in desc.split("·")]
        conf = next((x.split()[0] for x in parts if x.endswith("confidence")), "medium")
        reason = parts[-1] if len(parts) >= 3 else ""
        return {"confidence": conf if conf in ("low", "medium", "high") else "medium", "reason": reason}
    except Exception:
        return {}

def recover_after_restart():
    """
    Runs on startup. If the bot was restarted in the middle of a limit buy,
    cancel that leftover order and track anything it already bought.
    Open positions themselves live in Supabase, so they're picked up automatically.
    """
    client = get_coinbase_client()
    if not client:
        return
    try:
        resp = client.list_orders(order_status=["OPEN"])
        orders = [o for o in (_cb_get(resp, "orders", []) or [])
                  if str(_cb_get(o, "client_order_id", "")).startswith("lqb-")]
        if not orders:
            return
        users = get_all_users()
        user_id = users[0]["user_id"] if users else None
        for o in orders:
            oid = _cb_get(o, "order_id")
            pid = _cb_get(o, "product_id")
            client.cancel_orders(order_ids=[oid])
            log(f"   🧹 Cancelled leftover bot order {pid} ({str(oid)[:8]})")
            time.sleep(1)
            o = _cb_get(client.get_order(oid), "order", {})
            filled = float(_cb_get(o, "filled_size", 0) or 0)
            if filled > 0 and _cb_get(o, "side") == "BUY" and user_id:
                market = pid.split("-")[0]
                avg = float(_cb_get(o, "average_filled_price", 0) or 0)
                spent = float(_cb_get(o, "total_value_after_fees", 0) or 0) or filled * avg
                if not any(p.get("market") == market for p in get_open_positions(user_id)):
                    save_live_position(user_id, market, pid, avg, filled, spent)
                    log(f"   📝 Tracked partly-filled buy: {filled} {market} @ ${avg:.4f}")
    except Exception as e:
        log(f"   Restart recovery error: {e}")

def update_stop_price(position_id, new_stop):
    """Save the raised trailing stop so it survives restarts"""
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/live_positions?id=eq.{position_id}",
            json={"stop_loss_price": new_stop},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"})
    except Exception as e:
        log(f"  Stop update error: {e}")

def check_and_close_positions(user_id, prices):
    """Check all open positions and close if TP or SL hit"""
    positions = get_open_positions(user_id)
    if not positions:
        return 0, 0  # pnl, closed_count

    total_pnl = 0
    closed = 0

    for pos in positions:
        market = pos.get("market")
        product_id = pos.get("product_id")
        entry_price = float(pos.get("entry_price", 0))
        amount_coin = float(pos.get("amount_coin", 0))
        size_usdc = float(pos.get("size_usdc", 0))
        sl_price = float(pos.get("stop_loss_price", 0))
        tp_price = float(pos.get("take_profit_price", 0))
        pos_id = pos.get("id")

        # Get current price
        current_price = float(prices.get(market, 0))
        if current_price <= 0:
            continue

        # Trailing stop: once up TAKE_PROFIT_PCT, keep raising the stop under the price
        if entry_price > 0 and current_price >= entry_price * (1 + TAKE_PROFIT_PCT):
            new_stop = round(current_price * (1 - TRAIL_GAP_PCT), 6)
            if new_stop > sl_price:
                sl_price = new_stop
                update_stop_price(pos_id, new_stop)
                gain = (current_price / entry_price - 1) * 100
                log(f"  📈 {market} +{gain:.1f}% — trailing stop raised to ${new_stop:.4f} (locks ~{(new_stop/entry_price-1)*100:+.1f}%)")

        exit_reason = None
        if FORCE_CLOSE_ON_START and user_id not in _force_closed_users:
            exit_reason = "force close"
        elif current_price <= sl_price:
            exit_reason = "trailing stop" if sl_price > entry_price else "stop loss"

        if exit_reason:
            log(f"  🎯 {market} hit {exit_reason} @ ${current_price:.4f} (entry: ${entry_price:.4f})")
            # Real SELL by coin amount (base_size), capped at what this position bought
            result = sell_coinbase_position(product_id, max_coin=amount_coin)

            if result["status"] == "failed":
                # Keep position open so the next scan retries
                log(f"  ⏳ {market} still open — will retry sell next scan")
                continue

            if result["status"] == "nothing_to_sell":
                # Coins already gone (sold manually) or just dust — stop tracking it
                log(f"  🧹 {market}: nothing left to sell — marking closed")
                close_live_position(pos_id, "nothing to sell")
                closed += 1
                continue

            # Real P&L from the fill if we got it, otherwise estimate
            if result.get("filled_value"):
                net_pnl = round(result["filled_value"] - result.get("fees", 0) - size_usdc, 4)
                if result.get("avg_price"):
                    current_price = result["avg_price"]
            else:
                pnl = round((current_price - entry_price) / entry_price * size_usdc, 4)
                fee_cost = round(size_usdc * TRADING_FEE * 2, 4)
                net_pnl = round(pnl - fee_cost, 4)
            total_pnl += net_pnl
            closed += 1

            close_live_position(pos_id, exit_reason)
            meta = open_trade_meta.pop((user_id, market), None) or recover_trade_meta(user_id, market)
            supa_post("trades", {
                "user_id": user_id, "market": market, "side": "long",
                "price": f"${current_price:.4f}", "size_cad": round(size_usdc, 4),
                "leverage": 1, "pnl_cad": net_pnl,
                "confidence": meta.get("confidence", "medium"),
                "reason": f"[LIVE] {meta.get('reason', '')} · {exit_reason}"
            })
            supa_post("alerts", {
                "user_id": user_id,
                "type": "win" if net_pnl > 0 else "loss",
                "title": f"🔴 LIVE {market} SOLD — {'WIN ✓' if net_pnl > 0 else 'LOSS ✗'}",
                "description": f"Exit: {exit_reason} @ ${current_price:.4f} | P&L: {'+' if net_pnl >= 0 else ''}${net_pnl:.4f} USDC"
            })
            log(f"  {'WIN' if net_pnl > 0 else 'LOSS'}: {market} P&L = {'+' if net_pnl >= 0 else ''}${net_pnl:.4f} USDC")

    if FORCE_CLOSE_ON_START:
        _force_closed_users.add(user_id)
    return total_pnl, closed

# ---- EXECUTE TRADE (paper or live) ----
def execute_trade(user_id, market, side, size_cad, leverage, confidence, balance, price, price_str):
    """Execute a trade — paper simulation or real Coinbase order"""
    fee = TRADING_FEE
    slip = SLIPPAGE

    if LIVE_TRADING and COINBASE_API_KEY:
        # Skip LOW confidence
        if confidence == "low":
            log(f"  🔒 LIVE MODE: skipping low confidence")
            return None, None, None, "skipped — low confidence"

        # Coinbase spot only supports BUY
        if side == "short":
            log(f"  🔒 LIVE MODE: skipping SHORT — spot only supports BUY")
            return None, None, None, "skipped — no shorting"

        product_id = COINBASE_PAIRS.get(market)
        if not product_id:
            log(f"  {market} not in Coinbase pairs — skipping")
            return None, None, None, "market not on Coinbase"

        # Live balance is real USDC, so size is already USDC
        size_usdc = size_cad
        log(f"  💵 LIVE BUY: {product_id} ${size_usdc:.2f} USDC")
        order = place_limit_buy(product_id, size_usdc)

        if not order:
            return None, None, None, "buy didn't fill"

        # Use the real fill when we have it, otherwise estimate
        if order.get("filled_size") and order.get("avg_price"):
            amount_coin = order["filled_size"]
            price = order["avg_price"]
            size_usdc = order.get("spent", size_usdc)
        else:
            amount_coin = round(size_usdc / price, 6)
        fee_cost = round(size_usdc * fee, 4)

        # Save position for automatic exit later
        save_live_position(user_id, market, product_id, price, amount_coin, size_usdc)

        # Return 0 P&L for now — actual P&L comes when position closes
        log(f"  📈 Position opened: {amount_coin:.6f} {market} @ ${price:.4f} | TP: ${price*(1+TAKE_PROFIT_PCT):.4f} | SL: ${price*(1-STOP_LOSS_PCT):.4f}")
        return 0, fee_cost, "position opened", "live order placed ✅"

    else:
        # Paper simulation with trailing stop loss
        notional = size_cad * leverage
        fee_cost = notional * fee * 2
        slip_cost = notional * slip
        win_prob = 0.60 if confidence == "high" else 0.53 if confidence == "medium" else 0.45
        won = random.random() < win_prob

        if won:
            # Trailing stop — simulate price running in our favour
            # Price moves up, trailing stop follows, locks in profit
            initial_move = random.uniform(0.005, 0.06)  # initial move up to 6%
            # Trailing stop kicks in at 60% of peak — locks in at least 60% of gains
            trailing_capture = random.uniform(0.55, 0.85)  # capture 55-85% of move
            gross = notional * initial_move * trailing_capture
            exit_reason = "trailing stop"
            if initial_move > TAKE_PROFIT_PCT:
                exit_reason = "take profit + trailing"
        else:
            # Hard stop loss
            gross = -notional * random.uniform(0.005, STOP_LOSS_PCT)
            exit_reason = "stop loss"

        pnl = round(gross - fee_cost - slip_cost, 4)
        fees = round(fee_cost + slip_cost, 4)
        return pnl, fees, exit_reason, "paper trade"

# ---- VOLATILITY-BASED TRADE SIZING ----
def get_volatility(market_data):
    """Calculate market volatility from 24h range"""
    if not market_data:
        return 1.0  # assume normal
    high = market_data.get("high_24h", 0)
    low = market_data.get("low_24h", 0)
    current = market_data.get("current_price", 1)
    if current <= 0:
        return 1.0
    range_pct = ((high - low) / current) * 100
    return round(range_pct, 2)

def get_trade_size(base_pct, confidence, balance, market_data=None, fear_greed=None):
    """Dynamic trade size based on confidence, volatility and fear/greed"""
    # Live: size by confidence (low never trades)
    if LIVE_TRADING:
        pct = LIVE_SIZE_PCT.get(confidence, 0)
        if pct <= 0:
            return 0
    # Paper: old sizing
    elif confidence == "high":
        pct = min(base_pct + 5, 25)
    elif confidence == "low":
        pct = max(base_pct - 5, 5)
    else:
        pct = base_pct

    # Volatility adjustment — trade smaller when market is wild
    if market_data:
        vol = get_volatility(market_data)
        if vol > 15:       # very volatile — reduce by 40%
            pct = pct * 0.6
            log(f"    📉 High volatility ({vol:.1f}%) — reducing trade size")
        elif vol > 8:      # moderate volatility — reduce by 20%
            pct = pct * 0.8
        elif vol < 3:      # very calm — can increase slightly
            pct = pct * 1.1

    # Fear & Greed adjustment
    if fear_greed and fear_greed.get("value"):
        fg = fear_greed["value"]
        if fg <= 20:       # extreme fear — good buying opportunity, increase size on longs
            pct = pct * 1.15
            log(f"    😱 Extreme fear ({fg}) — slight size increase for contrarian trade")
        elif fg >= 80:     # extreme greed — risky, reduce size
            pct = pct * 0.75
            log(f"    🤑 Extreme greed ({fg}) — reducing size, market overextended")

    # Live mode cap
    if LIVE_TRADING:
        pct = min(pct, 25)

    pct = max(3, min(pct, 30))  # hard limits: 3% min, 30% max
    return balance * (pct / 100)

# ---- FREE PRE-FILTER (no Claude cost) ----
def prefilter_signals(md, fear_greed=None):
    """Count simple bullish signals using the same rules Claude is given"""
    if not md:
        return 0, []
    hits = []
    if md["rsi"] < 45: hits.append(f"RSI {md['rsi']}")
    if md["price_position_pct"] < 30: hits.append("near 24h low")
    if md["volume_ratio"] > 1.0: hits.append(f"vol {md['volume_ratio']}x")
    if md["above_sma5"] and md["above_sma10"]: hits.append("uptrend")
    if fear_greed and fear_greed.get("value") and fear_greed["value"] < 40: hits.append("fear")
    return len(hits), hits

# ---- SCAN FOR ONE USER ----
def scan_for_user(user, prices, do_learning, full_scan=True):
    global user_learning
    user_id = user["user_id"]
    balance = float(user.get("balance_cad") or START_CAD)
    total_pnl = float(user.get("total_pnl_cad") or 0)
    trade_count = int(user.get("trade_count") or 0)
    trade_size_pct = int(user.get("trade_size_pct") or 15)
    leverage = int(user.get("leverage") or 3)
    risk = user.get("risk") or "Medium"
    bot_enabled = user.get("bot_enabled", True)

    if not bot_enabled:
        log(f"  User {user_id[:8]}... paused")
        if LIVE_TRADING:
            check_and_close_positions(user_id, prices)   # never leave a real position unmanaged
        return

    live_paused_today = False
    account_value = None
    if LIVE_TRADING:
        usdc, account_value = get_live_account_value(user_id, prices)
        if usdc is None:
            log("  ⚠️ Couldn't read Coinbase balance — skipping this scan")
            return
        balance = usdc
        log(f"  💼 Account: ${account_value:.2f} USDC total (${usdc:.2f} free)")

        # Daily loss from real closed trades — resumes by itself tomorrow
        today_pnl = get_live_pnl_today(user_id)
        if today_pnl <= -MAX_DAILY_LOSS_CAD:
            live_paused_today = True
            log(f"  🛑 Daily loss ${abs(today_pnl):.2f} hit — no new buys until tomorrow (still managing open positions)")

    # Hard stop (live = real total account value incl. open positions)
    stop_value = account_value if LIVE_TRADING else balance
    if stop_value <= HARD_STOP_BALANCE:
        if LIVE_TRADING:
            check_and_close_positions(user_id, prices)   # still exit open positions on TP/SL
        log(f"  ⛔ HARD STOP — ${stop_value:.2f}")
        supa_post("alerts", {"user_id": user_id, "type": "warn",
            "title": "⛔ Hard stop triggered!",
            "description": f"Balance ${stop_value:.2f} — bot paused."})
        requests.patch(f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
            json={"bot_enabled": False, "updated_at": datetime.now(timezone.utc).isoformat()},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"})
        return

    if balance < 5 and not LIVE_TRADING:
        log(f"  Balance too low")
        return

    # Self learning
    if do_learning:
        log(f"  🧠 Self-learning review...")
        trades = get_trade_history(user_id, 100)
        analysis = analyze_trades(trades)
        if analysis:
            # Load historical insights and merge
            saved = load_insights(user_id)
            user_learning[user_id] = build_learning_prompt(analysis, saved)
            # Save updated insights to Supabase for persistence
            save_insights(user_id, analysis)
            log(f"  🧠 wr={analysis['win_rate']}% | long={analysis['long_win_rate']}% | short={analysis['short_win_rate']}% | best={analysis['best_markets']}")
            supa_post("alerts", {"user_id": user_id, "type": "win",
                "title": f"🧠 Bot learned from {analysis['total_trades']} trades",
                "description": f"Win rate: {analysis['win_rate']}% | Long: {analysis['long_win_rate']}% | Short: {analysis['short_win_rate']}% | Best markets: {', '.join(analysis['best_markets'][:2])}"})
        else:
            # Load saved insights even if not enough recent trades
            saved = load_insights(user_id)
            if saved:
                user_learning[user_id] = build_learning_prompt(None, saved)
                log(f"  🧠 Loaded historical insights (not enough new trades yet)")
    elif user_id not in user_learning:
        # On startup, load saved insights immediately
        saved = load_insights(user_id)
        if saved:
            user_learning[user_id] = build_learning_prompt(None, saved)
            log(f"  🧠 Restored insights from Supabase")

    learning_context = user_learning.get(user_id, "")

    # Check and close any open live positions first
    if LIVE_TRADING:
        pos_pnl, pos_closed = check_and_close_positions(user_id, prices)
        if pos_closed > 0:
            balance = max(0, balance + pos_pnl)
            total_pnl += pos_pnl
            log(f"  Closed {pos_closed} position(s) | P&L: {'+' if pos_pnl >= 0 else ''}${pos_pnl:.4f} | Balance: ${balance:.2f}")

    if LIVE_TRADING and live_paused_today:
        save_settings(user_id, (account_value if LIVE_TRADING and account_value is not None else balance), total_pnl, trade_count, trade_size_pct, leverage, risk)
        return

    if LIVE_TRADING and balance < 5:
        log(f"  Free USDC too low (${balance:.2f}) — waiting")
        save_settings(user_id, (account_value if LIVE_TRADING and account_value is not None else balance), total_pnl, trade_count, trade_size_pct, leverage, risk)
        return

    # Don't open new position if we already have one open
    if LIVE_TRADING:
        open_pos = get_open_positions(user_id)
        if open_pos:
            log(f"  ⏳ {len(open_pos)} position(s) open — waiting for exit before new trade")
            save_settings(user_id, (account_value if LIVE_TRADING and account_value is not None else balance), total_pnl, trade_count, trade_size_pct, leverage, risk)
            return

    # Between full scans: only manage positions (free), don't look for buys
    if not full_scan:
        log(f"  ✓ Positions checked — next buy search in a few min")
        save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk)
        return

    # Select markets
    available = [m for m in MARKETS if m in prices]
    if LIVE_TRADING:
        available = [m for m in available if m in COINBASE_PAIRS]  # only coins we can actually buy
    if not available:
        return

    # Bias toward learned good markets
    good_markets = []
    if learning_context:
        trades = get_trade_history(user_id, 30)
        analysis = analyze_trades(trades)
        if analysis and analysis["best_markets"]:
            good_markets = [m for m in analysis["best_markets"] if m in available]

    mode_tag = "🔴 LIVE" if LIVE_TRADING else "📄 PAPER"

    # Fetch fear & greed index once per scan (cached hourly)
    fear_greed = fetch_fear_greed()
    fg_str = f"Fear & Greed: {fear_greed['value']} ({fear_greed['label']})" if fear_greed.get("value") else ""
    if fg_str:
        log(f"  {fg_str}")

    # Free check on EVERY tradable coin, only the best go to Claude
    md_cache = {}
    ranked = []
    for m in available:
        md = fetch_market_data(m)
        md_cache[m] = md
        n, hits = prefilter_signals(md, fear_greed)
        if n >= MIN_PREFILTER_SIGNALS:
            ranked.append((n, m in good_markets, m, hits))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [m for _, _, m, _ in ranked[:MAX_CLAUDE_CALLS]]

    if not selected:
        log(f"  {mode_tag} | Checked {len(available)} coins for free — nothing interesting, no Claude calls")
        save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk)
        return
    log(f"  {mode_tag} | Checked {len(available)} coins for free — asking Claude about: "
        + ", ".join(f"{m} ({', '.join(h)})" for _, _, m, h in ranked[:MAX_CLAUDE_CALLS]))

    traded_this_scan = False
    for market in selected:
        if traded_this_scan:
            break
        price = float(prices[market])
        price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"
        log(f"    {market} @ {price_str}")

        market_data = md_cache.get(market) or fetch_market_data(market)
        funding = fetch_funding_rate(market)
        orderbook = fetch_orderbook(market)

        if market_data:
            vol = get_volatility(market_data)
            log(f"    RSI={market_data['rsi']} | Vol={market_data['volume_ratio']}x | OB={orderbook['bid_ask_ratio'] if orderbook else 'N/A'} | Range={vol:.1f}%")

        # Add fear & greed to learning context for Claude
        fg_context = f"\nMarket sentiment: {fg_str}" if fg_str else ""
        full_context = learning_context + fg_context

        decision = call_claude(market, price_str, risk, 1 if LIVE_TRADING else leverage, market_data, full_context, funding, orderbook)

        if decision.get("trade") and not traded_this_scan:
            side = decision.get("side", "long")
            confidence = decision.get("confidence", "medium")
            # Pass market_data and fear_greed for volatility-adjusted sizing
            size_cad = get_trade_size(trade_size_pct, confidence, balance, market_data, fear_greed)

            pnl, fees, exit_reason, order_status = execute_trade(
                user_id, market, side, size_cad, leverage, confidence, balance, price, price_str)

            if pnl is None:
                log(f"    {market} SKIP — {order_status}")
                continue

            if LIVE_TRADING:
                # Real buy opened — result gets recorded when it sells, not now
                traded_this_scan = True
                open_trade_meta[(user_id, market)] = {"confidence": confidence, "reason": decision.get("reason", "")}
                supa_post("alerts", {
                    "user_id": user_id, "type": "win",
                    "title": f"🔴 LIVE {market} BOUGHT",
                    "description": f"${size_cad:.2f} USDC @ {price_str} · {confidence} confidence · {decision.get('reason','')[:120]}"
                })
                balance = max(0, balance - size_cad)
                trade_count += 1
                continue

            won = pnl > 0
            balance = max(0, balance + pnl)
            total_pnl += pnl
            trade_count += 1
            traded_this_scan = True

            daily_limit_hit = check_daily_loss(user_id, pnl)
            result = "WIN ✓" if won else "LOSS ✗"
            log(f"  [{mode_tag}] {market} {side.upper()} [{result}] size=${size_cad:.2f} | {exit_reason} | P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} | Bal: ${balance:.2f}")

            supa_post("trades", {
                "user_id": user_id, "market": market, "side": side,
                "price": price_str, "size_cad": round(size_cad, 4),
                "leverage": 1 if LIVE_TRADING else leverage,
                "pnl_cad": pnl, "confidence": confidence,
                "reason": f"{'[LIVE] ' if LIVE_TRADING else ''}{decision.get('reason','')} · {exit_reason} · {fg_str}"
            })
            supa_post("alerts", {
                "user_id": user_id, "type": "win" if won else "loss",
                "title": f"{'🔴 LIVE' if LIVE_TRADING else '📄 PAPER'} {market} {side.upper()} — {result}",
                "description": f"{decision.get('reason','')} · Exit: {exit_reason} · P&L: {'+' if pnl >= 0 else ''}${abs(pnl):.2f} CAD · {fg_str}"
            })

            if balance / START_CAD < 0.5:
                supa_post("alerts", {"user_id": user_id, "type": "warn",
                    "title": "⚠️ Balance below 50%",
                    "description": f"Balance at ${balance:.2f} CAD"})

            if daily_limit_hit:
                supa_post("alerts", {"user_id": user_id, "type": "warn",
                    "title": "🛑 Max daily loss reached",
                    "description": f"Bot paused for today to protect your account."})
                requests.patch(f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
                    json={"bot_enabled": False, "updated_at": datetime.now(timezone.utc).isoformat()},
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"})
                break
        else:
            log(f"    {market} SKIP — {decision.get('reason','no signal')[:60]}")

    save_settings(user_id, (account_value if LIVE_TRADING and account_value is not None else balance), total_pnl, trade_count, trade_size_pct, leverage, risk)

# ---- MAIN ----
def main():
    global scan_counter
    log("🤖 LiquidBot server started")
    log(f"   Mode: {'🔴 LIVE TRADING — REAL MONEY' if LIVE_TRADING else '📄 PAPER TRADING'}")
    log(f"   Supabase: {SUPABASE_URL}")
    log(f"   Claude AI: {'ENABLED ✓' if ANTHROPIC_KEY else 'fallback'}")
    log(f"   Position check: every {SCAN_INTERVAL}s | Buy search: every {SCAN_INTERVAL*FULL_SCAN_EVERY//60} min | Claude only when {MIN_PREFILTER_SIGNALS}+ free signals")
    log(f"   Fee: {TRADING_FEE*100:.2f}% | Slippage: {SLIPPAGE*100:.1f}%")
    log(f"   SL: {STOP_LOSS_PCT*100:.1f}% | Trailing stop from +{TAKE_PROFIT_PCT*100:.1f}% ({TRAIL_GAP_PCT*100:.1f}% gap) | Hard stop: ${HARD_STOP_BALANCE}")
    log(f"   Max daily loss: ${MAX_DAILY_LOSS_CAD} {'USDC' if LIVE_TRADING else 'CAD'}")

    # Test Coinbase connection
    if COINBASE_API_KEY:
        log("   Testing Coinbase connection...")
        bal = get_coinbase_balance()
        if bal is not None:
            log(f"   Coinbase: CONNECTED ✓ | Real balance: ${bal:.2f} USDC")
            if LIVE_TRADING and bal < 5:
                log(f"   ⚠️ WARNING: Coinbase balance too low — deposit funds first")
            # List available CAD pairs to verify
            try:
                client = get_coinbase_client()
                if client:
                    products = client.get_products()
                    prod_list = products.products if hasattr(products, 'products') else products.get('products', [])
                    cad_pairs = [p.product_id if hasattr(p, 'product_id') else p.get('product_id','')
                                for p in prod_list if 'USDC' in (p.product_id if hasattr(p, 'product_id') else p.get('product_id',''))]
                    log(f"   Available USDC pairs: {', '.join(sorted(cad_pairs)[:10])}")
                    # Update COINBASE_PAIRS to only use available pairs
                    global COINBASE_PAIRS
                    COINBASE_PAIRS = {}
                    for coin in MARKETS:
                        if f"{coin}-USDC" in cad_pairs:
                            COINBASE_PAIRS[coin] = f"{coin}-USDC"
                    log(f"   Configured pairs: {list(COINBASE_PAIRS.keys())}")
            except Exception as e:
                log(f"   Could not list products: {e}")
        else:
            log(f"   Coinbase: ❌ Connection failed — check API key/secret in Railway Variables")
    else:
        log("   Coinbase: not configured")
    log("")

    if LIVE_TRADING:
        recover_after_restart()
        for u in get_all_users():
            open_now = get_open_positions(u["user_id"])
            if open_now:
                log(f"   📂 Picked up {len(open_now)} open position(s): {', '.join(p.get('market','?') for p in open_now)}")

    t = threading.Thread(target=start_keep_alive, daemon=True)
    t.start()

    consecutive_errors = 0

    while True:
        try:
            scan_counter += 1
            do_learning = scan_counter % LEARN_EVERY == 0
            do_weekly = scan_counter % WEEKLY_REPORT_SCANS == 0 and scan_counter > 0
            log(f"--- Scan #{scan_counter} {'🧠' if do_learning else ''}{'📊' if do_weekly else ''} ---")

            prices = fetch_prices()
            if not prices:
                log("  No prices")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    log("  ⚠️ 3 errors — waiting 60s")
                    time.sleep(60)
                    consecutive_errors = 0
                time.sleep(SCAN_INTERVAL)
                continue

            consecutive_errors = 0
            users = get_all_users()
            active = [u for u in users if u.get("bot_enabled", True)]
            log(f"  {len(prices)} markets | {len(active)} user(s)")

            for user in active:
                try:
                    scan_for_user(user, prices, do_learning, full_scan=(scan_counter % FULL_SCAN_EVERY == 1 or FULL_SCAN_EVERY <= 1))
                    if do_weekly:
                        send_weekly_report(user["user_id"])
                except Exception as e:
                    log(f"  ⚠️ Error: {e}")

            log(f"--- Done. Sleeping {SCAN_INTERVAL}s ---\n")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("Stopped.")
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"⚠️ Error #{consecutive_errors}: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()

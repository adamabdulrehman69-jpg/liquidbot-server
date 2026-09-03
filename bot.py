import time
import random
import requests
import os
import json
from datetime import datetime, timezone

# ---- CONFIG ----
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jcwvfgiudhzdpmqibwji.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_dE-1zCOEwJzWHA3ujLyCdw_UPFvdKf5")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "180"))
CAD_USD = 0.74
START_CAD = 100.0
TRADING_FEE = 0.0004
SLIPPAGE = 0.0005

MARKETS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "ARB", "BNB", "XRP"]

# Store recent prices for trend analysis
price_history = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ---- SUPABASE ----
def supa_get(table, filters=""):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}?{filters}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    )
    return r.json() if r.ok else []

def supa_post(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        json=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
    )
    return r.ok

def save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
        json={
            "balance_cad": round(balance, 4),
            "total_pnl_cad": round(total_pnl, 4),
            "trade_count": trade_count,
            "trade_size_pct": trade_size_pct,
            "leverage": leverage,
            "risk": risk,
            "bot_enabled": True,
            "updated_at": datetime.now(timezone.utc).isoformat()
        },
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
    )
    if r.ok:
        log(f"  Settings saved — balance: ${balance:.2f} CAD")
    else:
        log(f"  Settings save failed: {r.status_code} {r.text}")

def get_all_users():
    rows = supa_get("bot_settings", "select=user_id,balance_cad,total_pnl_cad,trade_count,trade_size_pct,leverage,risk,bot_enabled")
    return rows if isinstance(rows, list) else []

# ---- LIVE PRICES ----
def fetch_prices():
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "allMids"},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        return r.json() if r.ok else {}
    except Exception as e:
        log(f"Price fetch error: {e}")
        return {}

# ---- FETCH REAL MARKET DATA ----
def fetch_market_data(market):
    """Fetch real candle data from Hyperliquid for technical analysis"""
    try:
        # Get recent candles (1h candles, last 24)
        now_ms = int(time.time() * 1000)
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": market,
                    "interval": "1h",
                    "startTime": now_ms - 24 * 60 * 60 * 1000,
                    "endTime": now_ms
                }
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
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
        prev = closes[-2]
        open_24h = closes[0]

        # Trend: price change over last 24h
        change_24h = ((current - open_24h) / open_24h) * 100

        # Short term momentum: last 3 candles
        momentum = closes[-1] - closes[-4] if len(closes) >= 4 else 0
        momentum_pct = (momentum / closes[-4]) * 100 if len(closes) >= 4 else 0

        # Simple moving averages
        sma5 = sum(closes[-5:]) / min(5, len(closes))
        sma10 = sum(closes[-10:]) / min(10, len(closes))

        # Volume trend
        avg_vol = sum(volumes[:-1]) / max(1, len(volumes) - 1)
        current_vol = volumes[-1]
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1

        # RSI (simplified)
        gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
        losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
        avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else sum(gains) / max(1, len(gains))
        avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else sum(losses) / max(1, len(losses))
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))

        # Support/resistance (simple high/low range)
        high_24h = max(highs)
        low_24h = min(lows)
        price_position = ((current - low_24h) / (high_24h - low_24h)) * 100 if high_24h != low_24h else 50

        return {
            "current_price": current,
            "change_24h_pct": round(change_24h, 2),
            "momentum_3h_pct": round(momentum_pct, 2),
            "sma5": round(sma5, 4),
            "sma10": round(sma10, 4),
            "above_sma5": current > sma5,
            "above_sma10": current > sma10,
            "rsi": round(rsi, 1),
            "volume_ratio": round(vol_ratio, 2),
            "high_24h": high_24h,
            "low_24h": low_24h,
            "price_position_pct": round(price_position, 1),
        }
    except Exception as e:
        log(f"  Market data error for {market}: {e}")
        return None

# ---- CLAUDE AI ----
def call_claude(market, price_str, risk, leverage, market_data):
    if not ANTHROPIC_KEY:
        trade_prob = 0.35 if risk == "High" else 0.25 if risk == "Medium" else 0.15
        side = random.choice(["long", "short"])
        return {
            "trade": random.random() < trade_prob,
            "side": side,
            "confidence": "low",
            "reason": "Fallback logic — no AI key."
        }
    try:
        if market_data:
            md = market_data
            trend = "UPTREND" if md["above_sma5"] and md["above_sma10"] else \
                    "DOWNTREND" if not md["above_sma5"] and not md["above_sma10"] else "SIDEWAYS"
            rsi_signal = "OVERSOLD (buy signal)" if md["rsi"] < 30 else \
                         "OVERBOUGHT (sell signal)" if md["rsi"] > 70 else "NEUTRAL"
            vol_signal = "HIGH (strong move likely)" if md["volume_ratio"] > 1.5 else \
                         "LOW (weak move)" if md["volume_ratio"] < 0.7 else "NORMAL"

            data_str = f"""
TECHNICAL DATA for {market}:
- Current price: {price_str}
- 24h change: {md['change_24h_pct']}%
- 3h momentum: {md['momentum_3h_pct']}%
- Trend (SMA5 vs SMA10): {trend}
- RSI (14): {md['rsi']} — {rsi_signal}
- Volume vs average: {md['volume_ratio']}x — {vol_signal}
- Price position in 24h range: {md['price_position_pct']}% (0%=at low, 100%=at high)
- 24h High: ${md['high_24h']:,.2f} | 24h Low: ${md['low_24h']:,.2f}"""
        else:
            data_str = f"Current price: {price_str} (no additional data available)"

        prompt = f"""You are an expert crypto trading bot analyzing {market} for a paper trade.

{data_str}

Settings: Risk={risk}, Leverage={leverage}x

Based on this technical data, decide whether to trade:
- RSI below 30 = oversold = consider LONG
- RSI above 70 = overbought = consider SHORT  
- Strong uptrend + high volume = consider LONG
- Strong downtrend + high volume = consider SHORT
- Sideways or unclear = SKIP the trade
- Only trade when you have at least 2 confirming signals

Respond ONLY in JSON, no other text:
{{"trade": true/false, "side": "long"/"short", "confidence": "low"/"medium"/"high", "reason": "one sentence citing the specific signals"}}"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            },
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        resp = r.json()
        if "error" in resp:
            log(f"  Claude API error: {resp['error']}")
            return {"trade": False, "side": "long", "confidence": "low", "reason": "API error."}
        text = resp["content"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        log(f"  Claude: trade={result.get('trade')} side={result.get('side')} conf={result.get('confidence')} | {result.get('reason','')[:60]}")
        return result
    except Exception as e:
        log(f"  Claude error: {e}")
        return {"trade": False, "side": "long", "confidence": "low", "reason": "AI error, skipping."}

# ---- SIMULATE REALISTIC P&L ----
def simulate_pnl(size_cad, leverage, confidence):
    notional = size_cad * leverage
    fee_cost = notional * TRADING_FEE * 2
    slippage_cost = notional * SLIPPAGE
    win_prob = 0.56 if confidence == "high" else 0.51 if confidence == "medium" else 0.45
    won = random.random() < win_prob
    if won:
        gross_pnl = notional * random.uniform(0.003, 0.018)
    else:
        gross_pnl = -notional * random.uniform(0.003, 0.014)
    net_pnl = gross_pnl - fee_cost - slippage_cost
    return round(net_pnl, 4), round(fee_cost + slippage_cost, 4)

# ---- SCAN FOR ONE USER ----
def scan_for_user(user, prices):
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
        return

    if balance < 5:
        log(f"  User {user_id[:8]}... balance too low")
        return

    available = [m for m in MARKETS if m in prices]
    if not available:
        return

    market = random.choice(available)
    price = float(prices[market])
    price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"

    log(f"  User {user_id[:8]}... analyzing {market} @ {price_str}")

    # Fetch real technical data
    market_data = fetch_market_data(market)
    if market_data:
        log(f"  RSI={market_data['rsi']} | 24h={market_data['change_24h_pct']}% | Vol={market_data['volume_ratio']}x")

    decision = call_claude(market, price_str, risk, leverage, market_data)

    if decision.get("trade"):
        side = decision.get("side", "long")
        confidence = decision.get("confidence", "medium")
        size_cad = balance * (trade_size_pct / 100)
        pnl, fees = simulate_pnl(size_cad, leverage, confidence)
        won = pnl > 0
        balance = max(0, balance + pnl)
        total_pnl += pnl
        trade_count += 1

        result = "WIN ✓" if won else "LOSS ✗"
        log(f"  {market} {side.upper()} [{result}] P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} CAD (fees: -${fees:.3f}) | Balance: ${balance:.2f} CAD")

        supa_post("trades", {
            "user_id": user_id, "market": market, "side": side,
            "price": price_str, "size_cad": round(size_cad, 4),
            "leverage": leverage, "pnl_cad": pnl,
            "confidence": confidence, "reason": decision.get("reason", "")
        })
        supa_post("alerts", {
            "user_id": user_id,
            "type": "win" if won else "loss",
            "title": f"{market} {side.upper()} — {result}",
            "description": f"{decision.get('reason', '')} · P&L: {'+' if pnl >= 0 else ''}${abs(pnl):.2f} CAD · Fees: -${fees:.3f}"
        })
        if balance / START_CAD < 0.5:
            supa_post("alerts", {
                "user_id": user_id, "type": "warn",
                "title": "⚠️ Balance below 50%",
                "description": f"Paper balance at ${balance:.2f} CAD — consider pausing"
            })
    else:
        log(f"  {market} SKIP — {decision.get('reason', 'no signal')}")

    save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk)

# ---- MAIN ----
def main():
    log("🤖 LiquidBot server started")
    log(f"   Supabase: {SUPABASE_URL}")
    log(f"   Claude AI: {'ENABLED ✓' if ANTHROPIC_KEY else 'fallback mode'}")
    log(f"   Scan interval: {SCAN_INTERVAL}s")
    log(f"   Trading fee: {TRADING_FEE*100:.2f}% | Slippage: {SLIPPAGE*100:.2f}%")
    log(f"   Mode: REAL technical analysis (RSI, SMA, volume)")
    log("")

    while True:
        log("--- Scan started ---")
        prices = fetch_prices()
        if not prices:
            log("  No prices, skipping")
            time.sleep(SCAN_INTERVAL)
            continue

        log(f"  Got prices for {len(prices)} markets")
        users = get_all_users()
        active = [u for u in users if u.get("bot_enabled", True)]
        log(f"  Found {len(active)} active user(s)")

        for user in active:
            try:
                scan_for_user(user, prices)
            except Exception as e:
                log(f"  Error: {e}")

        log(f"--- Scan complete. Sleeping {SCAN_INTERVAL}s ---\n")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()

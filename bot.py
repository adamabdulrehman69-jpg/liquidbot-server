import time
import random
import requests
import os
import json
import threading
from datetime import datetime, timezone
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---- KEEP-ALIVE WEB SERVER ----
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'LiquidBot is running OK')
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
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "180"))
START_CAD = 100.0
TRADING_FEE = 0.0004
SLIPPAGE = 0.0005
LEARN_EVERY = 5
WEEKLY_REPORT_SCANS = 336
MAX_DAILY_LOSS_CAD = 10.0
STOP_LOSS_PCT = 0.025
TAKE_PROFIT_PCT = 0.035
HARD_STOP_BALANCE = 80.0
MARKETS_PER_SCAN = 3  # scan 3 markets per cycle instead of 1

# Top 20 markets instead of 8
MARKETS = [
    "BTC", "ETH", "SOL", "AVAX", "LINK", "ARB", "BNB", "XRP",
    "DOGE", "ADA", "MATIC", "LTC", "NEAR", "APT", "OP",
    "INJ", "SUI", "TIA", "WIF", "JUP"
]

user_learning = {}
user_daily_pnl = {}
user_daily_reset = {}
scan_counter = 0

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ---- DAILY LOSS TRACKING ----
def check_daily_loss(user_id, pnl):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if user_daily_reset.get(user_id) != today:
        user_daily_pnl[user_id] = 0
        user_daily_reset[user_id] = today
        log(f"  📅 New day — daily P&L reset")
    user_daily_pnl[user_id] = user_daily_pnl.get(user_id, 0) + pnl
    if user_daily_pnl[user_id] <= -MAX_DAILY_LOSS_CAD:
        log(f"  🛑 MAX DAILY LOSS hit — ${abs(user_daily_pnl[user_id]):.2f} CAD lost today")
        return True
    return False

# ---- WEEKLY REPORT ----
def send_weekly_report(user_id):
    try:
        trades = supa_get("trades", f"user_id=eq.{user_id}&order=created_at.desc&limit=200&select=market,side,pnl_cad,confidence,created_at")
        if not trades or len(trades) < 5:
            return
        week_ago = datetime.now(timezone.utc).timestamp() - 7 * 24 * 60 * 60
        week_trades = [t for t in trades if datetime.fromisoformat(t['created_at'].replace('Z', '+00:00')).timestamp() > week_ago]
        if not week_trades:
            return
        total = len(week_trades)
        wins = sum(1 for t in week_trades if float(t.get('pnl_cad', 0)) > 0)
        win_rate = round(wins / total * 100, 1)
        total_pnl = sum(float(t.get('pnl_cad', 0)) for t in week_trades)
        sorted_trades = sorted(week_trades, key=lambda t: float(t.get('pnl_cad', 0)))
        best = sorted_trades[-1]
        market_pnl = defaultdict(float)
        for t in week_trades:
            market_pnl[t['market']] += float(t.get('pnl_cad', 0))
        best_market = max(market_pnl, key=market_pnl.get)
        emoji = "📈" if total_pnl >= 0 else "📉"
        supa_post("alerts", {
            "user_id": user_id, "type": "win" if total_pnl >= 0 else "loss",
            "title": f"{emoji} Weekly Performance Report",
            "description": f"{total} trades | Win rate: {win_rate}% | P&L: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} CAD | Best market: {best_market} | Best trade: +${float(best.get('pnl_cad',0)):.2f} CAD"
        })
        log(f"  📊 Weekly report sent: {win_rate}% win rate | {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} CAD")
    except Exception as e:
        log(f"  Weekly report error: {e}")

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
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal"
        }
    )
    return r.ok

def save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
        json={
            "balance_cad": round(balance, 4), "total_pnl_cad": round(total_pnl, 4),
            "trade_count": trade_count, "trade_size_pct": trade_size_pct,
            "leverage": leverage, "risk": risk, "bot_enabled": True,
            "updated_at": datetime.now(timezone.utc).isoformat()
        },
        headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal"
        }
    )
    if r.ok:
        log(f"  Saved — balance: ${balance:.2f} CAD")
    else:
        log(f"  Save failed: {r.status_code}")

def get_all_users():
    rows = supa_get("bot_settings", "select=user_id,balance_cad,total_pnl_cad,trade_count,trade_size_pct,leverage,risk,bot_enabled")
    return rows if isinstance(rows, list) else []

# ---- LIVE PRICES ----
def fetch_prices():
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "allMids"},
            headers={"Content-Type": "application/json"}, timeout=10
        )
        return r.json() if r.ok else {}
    except Exception as e:
        log(f"Price fetch error: {e}")
        return {}

# ---- MARKET DATA (RSI, SMA, Volume) ----
def fetch_market_data(market):
    try:
        now_ms = int(time.time() * 1000)
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "candleSnapshot", "req": {
                "coin": market, "interval": "1h",
                "startTime": now_ms - 24 * 60 * 60 * 1000, "endTime": now_ms
            }},
            headers={"Content-Type": "application/json"}, timeout=10
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
            "current_price": current,
            "change_24h_pct": round(change_24h, 2),
            "momentum_3h_pct": round(momentum_pct, 2),
            "above_sma5": current > sma5,
            "above_sma10": current > sma10,
            "rsi": round(rsi, 1),
            "volume_ratio": round(vol_ratio, 2),
            "high_24h": high_24h, "low_24h": low_24h,
            "price_position_pct": round(price_position, 1),
        }
    except Exception as e:
        log(f"  Market data error {market}: {e}")
        return None

# ---- FUNDING RATE ----
def fetch_funding_rate(market):
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            headers={"Content-Type": "application/json"}, timeout=10
        )
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
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "l2Book", "coin": market},
            headers={"Content-Type": "application/json"}, timeout=10
        )
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

# ---- SELF LEARNING ----
def get_trade_history(user_id, limit=30):
    rows = supa_get("trades", f"user_id=eq.{user_id}&order=created_at.desc&limit={limit}&select=market,side,pnl_cad,confidence,reason")
    return rows if isinstance(rows, list) else []

def analyze_trades(trades):
    if len(trades) < 5:
        return None
    market_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    confidence_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    winning_reasons = []
    losing_reasons = []
    for t in trades:
        market = t.get("market", "?")
        pnl = float(t.get("pnl_cad", 0))
        confidence = t.get("confidence", "?")
        reason = t.get("reason", "")
        won = pnl > 0
        market_stats[market]["wins" if won else "losses"] += 1
        market_stats[market]["pnl"] += pnl
        confidence_stats[confidence]["wins" if won else "losses"] += 1
        if won and reason:
            winning_reasons.append(reason[:80])
        elif not won and reason:
            losing_reasons.append(reason[:80])
    best_markets = sorted(market_stats.keys(), key=lambda m: market_stats[m]["pnl"], reverse=True)[:3]
    worst_markets = sorted(market_stats.keys(), key=lambda m: market_stats[m]["pnl"])[:3]
    total_wins = sum(1 for t in trades if float(t.get("pnl_cad", 0)) > 0)
    win_rate = round(total_wins / len(trades) * 100, 1)
    market_wr = {}
    for m, s in market_stats.items():
        total = s["wins"] + s["losses"]
        if total > 0:
            market_wr[m] = round(s["wins"] / total * 100, 1)
    return {
        "total_trades": len(trades), "win_rate": win_rate,
        "best_markets": best_markets, "worst_markets": worst_markets,
        "market_win_rates": market_wr,
        "winning_reasons": winning_reasons[-3:],
        "losing_reasons": losing_reasons[-3:],
        "confidence_stats": dict(confidence_stats),
    }

def build_learning_prompt(analysis):
    if not analysis:
        return ""
    mwr = analysis["market_win_rates"]
    good_markets = [m for m, wr in mwr.items() if wr >= 55]
    bad_markets = [m for m, wr in mwr.items() if wr < 40]
    lines = [f"\n🧠 SELF-LEARNING (from last {analysis['total_trades']} trades, win rate: {analysis['win_rate']}%):"]
    if good_markets:
        lines.append(f"- PREFER: {', '.join(good_markets)} (historically profitable)")
    if bad_markets:
        lines.append(f"- AVOID: {', '.join(bad_markets)} (historically losing)")
    cs = analysis["confidence_stats"]
    if "low" in cs:
        l = cs["low"]
        total = l["wins"] + l["losses"]
        if total > 0 and round(l["wins"]/total*100) < 45:
            lines.append(f"- Skip low confidence trades — losing {100-round(l['wins']/total*100)}% of the time")
    if analysis["winning_reasons"]:
        lines.append(f"- Winning patterns: {' | '.join(analysis['winning_reasons'][:2])}")
    return "\n".join(lines)

# ---- CLAUDE AI ----
def call_claude(market, price_str, risk, leverage, market_data, learning_context="", funding=None, orderbook=None):
    if not ANTHROPIC_KEY:
        # Smarter fallback — use actual market data signals
        if market_data:
            rsi = market_data["rsi"]
            vol = market_data["volume_ratio"]
            trend = market_data["above_sma5"] and market_data["above_sma10"]
            # Count signals
            signals = 0
            side = "long"
            if rsi < 35:
                signals += 1
                side = "long"
            elif rsi > 65:
                signals += 1
                side = "short"
            if vol > 1.0:
                signals += 1
            if trend:
                signals += 0.5
                side = "long"
            elif not market_data["above_sma5"] and not market_data["above_sma10"]:
                signals += 0.5
                side = "short"
            if orderbook and orderbook["bid_ask_ratio"] > 1.2:
                signals += 0.5
                side = "long"
            elif orderbook and orderbook["bid_ask_ratio"] < 0.8:
                signals += 0.5
                side = "short"
            should_trade = signals >= 1.5
            conf = "high" if signals >= 2.5 else "medium" if signals >= 1.5 else "low"
            return {
                "trade": should_trade, "side": side, "confidence": conf,
                "reason": f"Fallback: RSI={rsi}, vol={vol:.1f}x, signals={signals:.1f}"
            }
        return {"trade": False, "side": "long", "confidence": "low", "reason": "No data available"}

    try:
        if market_data:
            md = market_data
            trend = "UPTREND" if md["above_sma5"] and md["above_sma10"] else \
                    "DOWNTREND" if not md["above_sma5"] and not md["above_sma10"] else "SIDEWAYS"
            rsi_signal = "OVERSOLD — strong LONG signal" if md["rsi"] < 30 else \
                         "APPROACHING oversold — consider LONG" if md["rsi"] < 40 else \
                         "APPROACHING overbought — consider SHORT" if md["rsi"] > 60 else \
                         "OVERBOUGHT — strong SHORT signal" if md["rsi"] > 70 else "NEUTRAL"
            vol_signal = "HIGH — strong move likely" if md["volume_ratio"] > 1.2 else \
                         "MODERATE" if md["volume_ratio"] > 0.8 else "LOW — weak move"
            data_str = f"""
TECHNICAL DATA for {market}:
- Price: {price_str} | 24h change: {md['change_24h_pct']}%
- Trend: {trend} | 3h momentum: {md['momentum_3h_pct']}%
- RSI(14): {md['rsi']} — {rsi_signal}
- Volume: {md['volume_ratio']}x average — {vol_signal}
- Price in 24h range: {md['price_position_pct']}% (0=at low, 100=at high)"""
            if funding:
                data_str += f"\n- Funding: {funding['funding_rate']}% ({funding['funding_signal']})"
            if orderbook:
                data_str += f"\n- Order book: {orderbook['bid_ask_ratio']} ratio ({orderbook['orderbook_signal']})"
        else:
            data_str = f"Price: {price_str} (limited data)"

        # Dynamic confidence threshold based on risk
        threshold = "1 strong signal or 2 moderate signals" if risk == "High" else \
                    "1.5 confirming signals" if risk == "Medium" else "2+ confirming signals"

        prompt = f"""You are an expert crypto trading bot analyzing {market}.

{data_str}
{learning_context}

Risk: {risk} | Leverage: {leverage}x
Trade threshold: {threshold}

Signal guide:
- RSI <35 or >65 = strong signal | RSI <40 or >60 = moderate signal
- Volume >1.2x = confirms move | Volume >0.8x = moderate confirmation  
- Funding negative = bullish | Funding positive = bearish
- Order book ratio >1.2 = bullish | <0.8 = bearish
- Trend direction adds 0.5 signal

Be willing to trade on moderate setups — the stop loss protects downside.
Apply self-learning insights to prefer good markets and avoid bad ones.

Respond ONLY in JSON:
{{"trade": true/false, "side": "long"/"short", "confidence": "low"/"medium"/"high", "reason": "cite specific signals"}}"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"}, timeout=15
        )
        resp = r.json()
        if "error" in resp:
            log(f"  Claude error: {resp['error'].get('message','?')[:60]}")
            return {"trade": False, "side": "long", "confidence": "low", "reason": "API error"}
        text = resp["content"][0]["text"].replace("```json","").replace("```","").strip()
        result = json.loads(text)
        log(f"  Claude: {market} trade={result.get('trade')} side={result.get('side')} conf={result.get('confidence')} | {result.get('reason','')[:70]}")
        return result
    except Exception as e:
        log(f"  Claude error: {e}")
        return {"trade": False, "side": "long", "confidence": "low", "reason": "Error"}

# ---- SIMULATE P&L with stop loss / take profit ----
def simulate_pnl(size_cad, leverage, confidence):
    notional = size_cad * leverage
    fee_cost = notional * TRADING_FEE * 2
    slippage_cost = notional * SLIPPAGE
    # Higher win prob for high confidence
    win_prob = 0.60 if confidence == "high" else 0.53 if confidence == "medium" else 0.45
    won = random.random() < win_prob
    if won:
        # Dynamic take profit — hold winners longer on high confidence
        tp = TAKE_PROFIT_PCT * 1.5 if confidence == "high" else TAKE_PROFIT_PCT
        gross_pnl = notional * random.uniform(0.005, tp)
        exit_reason = "take profit"
    else:
        gross_pnl = -notional * random.uniform(0.005, STOP_LOSS_PCT)
        exit_reason = "stop loss"
    return round(gross_pnl - fee_cost - slippage_cost, 4), round(fee_cost + slippage_cost, 4), exit_reason

# ---- DYNAMIC TRADE SIZE based on confidence ----
def get_trade_size(base_pct, confidence, balance):
    if confidence == "high":
        pct = min(base_pct + 5, 25)  # add 5% on high confidence, max 25%
    elif confidence == "low":
        pct = max(base_pct - 5, 5)   # reduce 5% on low confidence, min 5%
    else:
        pct = base_pct
    return balance * (pct / 100)

# ---- SCAN ONE MARKET for a user ----
def scan_one_market(user_id, market, price, prices, trade_size_pct, leverage, risk, learning_context):
    price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"
    log(f"    {market} @ {price_str}")
    market_data = fetch_market_data(market)
    funding = fetch_funding_rate(market)
    orderbook = fetch_orderbook(market)
    if market_data:
        log(f"    RSI={market_data['rsi']} | Vol={market_data['volume_ratio']}x | OB={orderbook['bid_ask_ratio'] if orderbook else 'N/A'}")
    return call_claude(market, price_str, risk, leverage, market_data, learning_context, funding, orderbook)

# ---- SCAN FOR ONE USER ----
def scan_for_user(user, prices, do_learning):
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
        return

    if balance <= HARD_STOP_BALANCE:
        log(f"  ⛔ HARD STOP — ${balance:.2f} CAD below ${HARD_STOP_BALANCE}")
        supa_post("alerts", {"user_id": user_id, "type": "warn",
            "title": "⛔ Hard stop triggered!",
            "description": f"Balance at ${balance:.2f} CAD — bot paused automatically."})
        requests.patch(f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
            json={"bot_enabled": False, "updated_at": datetime.now(timezone.utc).isoformat()},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"})
        return

    if balance < 5:
        log(f"  User {user_id[:8]}... balance too low")
        return

    # Self learning
    if do_learning:
        log(f"  🧠 Running self-learning review...")
        trades = get_trade_history(user_id, 30)
        analysis = analyze_trades(trades)
        if analysis:
            user_learning[user_id] = build_learning_prompt(analysis)
            log(f"  🧠 Learned: wr={analysis['win_rate']}% | best={analysis['best_markets']} | worst={analysis['worst_markets']}")
            supa_post("alerts", {"user_id": user_id, "type": "win",
                "title": f"🧠 Bot learned from {analysis['total_trades']} trades",
                "description": f"Win rate: {analysis['win_rate']}% | Best: {', '.join(analysis['best_markets'])} | Avoiding: {', '.join(analysis['worst_markets'])}"})

    learning_context = user_learning.get(user_id, "")

    # Pick markets — bias toward learned good markets
    available = [m for m in MARKETS if m in prices]
    if not available:
        return

    # Select MARKETS_PER_SCAN markets to analyze this cycle
    good_markets = []
    if learning_context:
        trades = get_trade_history(user_id, 30)
        analysis = analyze_trades(trades)
        if analysis and analysis["best_markets"]:
            good_markets = [m for m in analysis["best_markets"] if m in available]

    selected = []
    # Add 1-2 good markets if we have them
    if good_markets:
        selected += random.sample(good_markets, min(2, len(good_markets)))
    # Fill rest with random markets
    remaining = [m for m in available if m not in selected]
    needed = MARKETS_PER_SCAN - len(selected)
    if remaining and needed > 0:
        selected += random.sample(remaining, min(needed, len(remaining)))

    log(f"  User {user_id[:8]}... scanning {len(selected)} markets: {', '.join(selected)}")

    traded_this_scan = False
    for market in selected:
        if traded_this_scan:
            break  # only one trade per scan max
        price = float(prices[market])
        decision = scan_one_market(user_id, market, price, prices, trade_size_pct, leverage, risk, learning_context)

        if decision.get("trade") and not traded_this_scan:
            side = decision.get("side", "long")
            confidence = decision.get("confidence", "medium")
            price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"

            # Dynamic trade size based on confidence
            size_cad = get_trade_size(trade_size_pct, confidence, balance)
            pnl, fees, exit_reason = simulate_pnl(size_cad, leverage, confidence)
            won = pnl > 0
            balance = max(0, balance + pnl)
            total_pnl += pnl
            trade_count += 1
            traded_this_scan = True

            daily_limit_hit = check_daily_loss(user_id, pnl)
            result = "WIN ✓" if won else "LOSS ✗"
            log(f"  {market} {side.upper()} [{result}] size=${size_cad:.2f} | Exit: {exit_reason} | P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} CAD | Balance: ${balance:.2f} CAD")

            supa_post("trades", {
                "user_id": user_id, "market": market, "side": side,
                "price": price_str, "size_cad": round(size_cad, 4),
                "leverage": leverage, "pnl_cad": pnl,
                "confidence": confidence, "reason": decision.get("reason", "")
            })
            supa_post("alerts", {
                "user_id": user_id, "type": "win" if won else "loss",
                "title": f"{market} {side.upper()} — {result}",
                "description": f"{decision.get('reason','')} · Exit: {exit_reason} · P&L: {'+' if pnl >= 0 else ''}${abs(pnl):.2f} CAD · Fees: -${fees:.3f}"
            })

            if balance / START_CAD < 0.5:
                supa_post("alerts", {"user_id": user_id, "type": "warn",
                    "title": "⚠️ Balance below 50%",
                    "description": f"Paper balance at ${balance:.2f} CAD"})

            if daily_limit_hit:
                supa_post("alerts", {"user_id": user_id, "type": "warn",
                    "title": "🛑 Max daily loss reached",
                    "description": f"Lost ${MAX_DAILY_LOSS_CAD:.0f}+ CAD today — bot paused until tomorrow."})
                requests.patch(f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
                    json={"bot_enabled": False, "updated_at": datetime.now(timezone.utc).isoformat()},
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"})
                break
        else:
            price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"
            log(f"    {market} SKIP — {decision.get('reason','no signal')[:60]}")

    save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk)

# ---- MAIN ----
def main():
    global scan_counter
    log("🤖 LiquidBot server started")
    log(f"   Supabase: {SUPABASE_URL}")
    log(f"   Claude AI: {'ENABLED ✓' if ANTHROPIC_KEY else 'fallback mode'}")
    log(f"   Scan interval: {SCAN_INTERVAL}s | Markets per scan: {MARKETS_PER_SCAN}")
    log(f"   Total markets: {len(MARKETS)} | Fee: {TRADING_FEE*100:.2f}% | Slippage: {SLIPPAGE*100:.2f}%")
    log(f"   Stop loss: {STOP_LOSS_PCT*100:.1f}% | Take profit: {TAKE_PROFIT_PCT*100:.1f}% | Hard stop: ${HARD_STOP_BALANCE}")
    log(f"   Max daily loss: ${MAX_DAILY_LOSS_CAD} CAD | Self-learning: every {LEARN_EVERY} scans")
    log(f"   Mode: RSI + SMA + Volume + Funding + OB + Self Learning + Dynamic Sizing 🧠")

    t = threading.Thread(target=start_keep_alive, daemon=True)
    t.start()
    log("")

    consecutive_errors = 0

    while True:
        try:
            scan_counter += 1
            do_learning = scan_counter % LEARN_EVERY == 0
            do_weekly = scan_counter % WEEKLY_REPORT_SCANS == 0 and scan_counter > 0
            log(f"--- Scan #{scan_counter} {'🧠' if do_learning else ''}{'📊' if do_weekly else ''} ---")

            prices = fetch_prices()
            if not prices:
                log("  No prices, skipping")
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
            log(f"  {len(prices)} markets | {len(active)} active user(s)")

            for user in active:
                try:
                    scan_for_user(user, prices, do_learning)
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

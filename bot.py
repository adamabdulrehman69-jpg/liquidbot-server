import time
import random
import requests
import os
import json
from datetime import datetime, timezone
from collections import defaultdict

# ---- CONFIG ----
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jcwvfgiudhzdpmqibwji.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_dE-1zCOEwJzWHA3ujLyCdw_UPFvdKf5")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "180"))
START_CAD = 100.0
TRADING_FEE = 0.0004
SLIPPAGE = 0.0005
LEARN_EVERY = 5  # review trades every 5 scans

MARKETS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "ARB", "BNB", "XRP"]

# In-memory learning state per user
user_learning = {}
scan_counter = 0

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

# ---- FETCH MARKET DATA ----
def fetch_market_data(market):
    try:
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
        open_24h = closes[0]
        change_24h = ((current - open_24h) / open_24h) * 100
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
            "high_24h": high_24h,
            "low_24h": low_24h,
            "price_position_pct": round(price_position, 1),
        }
    except Exception as e:
        log(f"  Market data error for {market}: {e}")
        return None

# ---- FUNDING RATES ----
def fetch_funding_rate(market):
    """Fetch real funding rate from Hyperliquid — negative = shorts pay longs (bullish), positive = longs pay shorts (bearish)"""
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if not r.ok:
            return None
        data = r.json()
        # data is [meta, asset_contexts]
        if len(data) < 2:
            return None
        meta = data[0]
        ctxs = data[1]
        coins = [a["name"] for a in meta.get("universe", [])]
        if market not in coins:
            return None
        idx = coins.index(market)
        ctx = ctxs[idx]
        funding = float(ctx.get("funding", 0))
        open_interest = float(ctx.get("openInterest", 0))
        return {
            "funding_rate": round(funding * 100, 4),  # as percentage
            "open_interest": round(open_interest, 2),
            "funding_signal": "BEARISH (longs pay)" if funding > 0.0001 else "BULLISH (shorts pay)" if funding < -0.0001 else "NEUTRAL"
        }
    except Exception as e:
        log(f"  Funding rate error for {market}: {e}")
        return None

# ---- ORDER BOOK DEPTH ----
def fetch_orderbook(market):
    """Fetch order book to see buy vs sell pressure"""
    try:
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "l2Book", "coin": market},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if not r.ok:
            return None
        data = r.json()
        levels = data.get("levels", [[], []])
        if len(levels) < 2:
            return None

        bids = levels[0][:10]  # top 10 bids
        asks = levels[1][:10]  # top 10 asks

        bid_volume = sum(float(b["sz"]) for b in bids)
        ask_volume = sum(float(a["sz"]) for a in asks)
        total = bid_volume + ask_volume

        bid_pct = round(bid_volume / total * 100, 1) if total > 0 else 50
        ask_pct = round(ask_volume / total * 100, 1) if total > 0 else 50

        # Bid/ask ratio > 1 = more buyers = bullish
        ratio = round(bid_volume / ask_volume, 2) if ask_volume > 0 else 1

        return {
            "bid_pct": bid_pct,
            "ask_pct": ask_pct,
            "bid_ask_ratio": ratio,
            "orderbook_signal": "BULLISH (more buyers)" if ratio > 1.2 else "BEARISH (more sellers)" if ratio < 0.8 else "BALANCED"
        }
    except Exception as e:
        log(f"  Order book error for {market}: {e}")
        return None
def get_trade_history(user_id, limit=30):
    """Fetch recent trades from Supabase for learning"""
    rows = supa_get("trades", f"user_id=eq.{user_id}&order=created_at.desc&limit={limit}&select=market,side,pnl_cad,confidence,reason")
    return rows if isinstance(rows, list) else []

def analyze_trades(trades):
    """Analyze trade history to find patterns"""
    if len(trades) < 5:
        return None

    market_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    side_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    confidence_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    winning_reasons = []
    losing_reasons = []

    for t in trades:
        market = t.get("market", "?")
        side = t.get("side", "?")
        pnl = float(t.get("pnl_cad", 0))
        confidence = t.get("confidence", "?")
        reason = t.get("reason", "")
        won = pnl > 0

        market_stats[market]["wins" if won else "losses"] += 1
        market_stats[market]["pnl"] += pnl
        side_stats[side]["wins" if won else "losses"] += 1
        confidence_stats[confidence]["wins" if won else "losses"] += 1

        if won and reason:
            winning_reasons.append(reason[:80])
        elif not won and reason:
            losing_reasons.append(reason[:80])

    # Find best and worst markets
    best_markets = sorted(market_stats.keys(), key=lambda m: market_stats[m]["pnl"], reverse=True)[:3]
    worst_markets = sorted(market_stats.keys(), key=lambda m: market_stats[m]["pnl"])[:3]

    # Win rates
    total_wins = sum(1 for t in trades if float(t.get("pnl_cad", 0)) > 0)
    win_rate = round(total_wins / len(trades) * 100, 1)

    # Market win rates
    market_wr = {}
    for m, s in market_stats.items():
        total = s["wins"] + s["losses"]
        if total > 0:
            market_wr[m] = round(s["wins"] / total * 100, 1)

    return {
        "total_trades": len(trades),
        "win_rate": win_rate,
        "best_markets": best_markets,
        "worst_markets": worst_markets,
        "market_win_rates": market_wr,
        "winning_reasons": winning_reasons[-3:],
        "losing_reasons": losing_reasons[-3:],
        "side_stats": dict(side_stats),
        "confidence_stats": dict(confidence_stats),
    }

def build_learning_prompt(analysis):
    """Build a learning summary to feed back to Claude"""
    if not analysis:
        return ""

    mwr = analysis["market_win_rates"]
    good_markets = [m for m, wr in mwr.items() if wr >= 55]
    bad_markets = [m for m, wr in mwr.items() if wr < 40]

    lines = [
        f"\n🧠 SELF-LEARNING INSIGHTS (from last {analysis['total_trades']} trades, win rate: {analysis['win_rate']}%):"
    ]

    if good_markets:
        lines.append(f"- PREFER trading: {', '.join(good_markets)} (historically profitable)")
    if bad_markets:
        lines.append(f"- AVOID trading: {', '.join(bad_markets)} (historically losing)")

    cs = analysis["confidence_stats"]
    if "high" in cs:
        h = cs["high"]
        total = h["wins"] + h["losses"]
        if total > 0:
            lines.append(f"- High confidence trades win {round(h['wins']/total*100)}% of the time")
    if "low" in cs:
        l = cs["low"]
        total = l["wins"] + l["losses"]
        if total > 0 and round(l["wins"]/total*100) < 45:
            lines.append(f"- Low confidence trades are losing — skip them")

    if analysis["winning_reasons"]:
        lines.append(f"- Winning patterns: {' | '.join(analysis['winning_reasons'][:2])}")
    if analysis["losing_reasons"]:
        lines.append(f"- Losing patterns to avoid: {' | '.join(analysis['losing_reasons'][:2])}")

    return "\n".join(lines)

# ---- CLAUDE AI ----
def call_claude(market, price_str, risk, leverage, market_data, learning_context="", funding=None, orderbook=None):
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
- Price position in 24h range: {md['price_position_pct']}% (0%=at low, 100%=at high)"""

            # Add funding rate data
            if funding:
                data_str += f"""
- Funding rate: {funding['funding_rate']}% — {funding['funding_signal']}
- Open interest: ${funding['open_interest']:,.0f}"""

            # Add order book data
            if orderbook:
                data_str += f"""
- Order book: {orderbook['bid_pct']}% bids vs {orderbook['ask_pct']}% asks (ratio: {orderbook['bid_ask_ratio']}) — {orderbook['orderbook_signal']}"""
        else:
            data_str = f"Current price: {price_str} (no additional data available)"

        prompt = f"""You are an expert crypto trading bot analyzing {market} for a paper trade.

{data_str}
{learning_context}

Settings: Risk={risk}, Leverage={leverage}x

Rules:
- RSI below 30 = oversold = consider LONG
- RSI above 70 = overbought = consider SHORT
- Strong trend + high volume = trade in trend direction
- Negative funding rate = bullish signal (shorts paying longs)
- Positive funding rate = bearish signal (longs paying shorts)
- Order book ratio > 1.2 = more buyers = bullish
- Order book ratio < 0.8 = more sellers = bearish
- Sideways or unclear = SKIP
- Apply self-learning insights above — prefer good markets, avoid bad ones
- Only trade with 2+ confirming signals across RSI, trend, volume, funding, and order book
- Skip low confidence trades if history shows they lose

Respond ONLY in JSON, no other text:
{{"trade": true/false, "side": "long"/"short", "confidence": "low"/"medium"/"high", "reason": "one sentence citing specific signals including funding/orderbook if relevant"}}"""

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
        log(f"  Claude: trade={result.get('trade')} side={result.get('side')} conf={result.get('confidence')} | {result.get('reason','')[:70]}")
        return result
    except Exception as e:
        log(f"  Claude error: {e}")
        return {"trade": False, "side": "long", "confidence": "low", "reason": "AI error, skipping."}

# ---- STOP LOSS / TAKE PROFIT / HARD STOP ----
STOP_LOSS_PCT = 0.025       # 2.5% loss on notional = stop loss triggers
TAKE_PROFIT_PCT = 0.035     # 3.5% gain on notional = take profit triggers
HARD_STOP_BALANCE = 80.0    # stop all trading if balance drops below $80 CAD

def simulate_pnl(size_cad, leverage, confidence, balance):
    """Simulate P&L with stop loss and take profit logic"""
    notional = size_cad * leverage
    fee_cost = notional * TRADING_FEE * 2
    slippage_cost = notional * SLIPPAGE

    win_prob = 0.57 if confidence == "high" else 0.51 if confidence == "medium" else 0.43
    won = random.random() < win_prob

    if won:
        # Take profit hits — capped at TAKE_PROFIT_PCT
        gross_pnl = notional * random.uniform(0.005, TAKE_PROFIT_PCT)
        exit_reason = "take profit"
    else:
        # Stop loss hits — capped at STOP_LOSS_PCT
        gross_pnl = -notional * random.uniform(0.005, STOP_LOSS_PCT)
        exit_reason = "stop loss"

    net_pnl = gross_pnl - fee_cost - slippage_cost
    return round(net_pnl, 4), round(fee_cost + slippage_cost, 4), exit_reason

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

    # Hard stop — if balance too low, pause bot automatically
    if balance <= HARD_STOP_BALANCE:
        log(f"  ⛔ HARD STOP — balance ${balance:.2f} CAD is below ${HARD_STOP_BALANCE} CAD threshold")
        supa_post("alerts", {
            "user_id": user_id,
            "type": "warn",
            "title": "⛔ Hard stop triggered!",
            "description": f"Balance dropped to ${balance:.2f} CAD (below ${HARD_STOP_BALANCE} CAD limit). Bot paused automatically to protect your account."
        })
        # Pause the bot
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/bot_settings?user_id=eq.{user_id}",
            json={"bot_enabled": False, "updated_at": datetime.now(timezone.utc).isoformat()},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"}
        )
        return

    if balance < 5:
        log(f"  User {user_id[:8]}... balance too low")
        return

    # Self learning — review trades every N scans
    if do_learning:
        log(f"  🧠 Running self-learning review...")
        trades = get_trade_history(user_id, limit=30)
        analysis = analyze_trades(trades)
        if analysis:
            user_learning[user_id] = build_learning_prompt(analysis)
            log(f"  🧠 Learned: win rate={analysis['win_rate']}% | best={analysis['best_markets']} | worst={analysis['worst_markets']}")
            # Save learning insight as alert
            supa_post("alerts", {
                "user_id": user_id,
                "type": "win",
                "title": f"🧠 Bot learned from {analysis['total_trades']} trades",
                "description": f"Win rate: {analysis['win_rate']}% | Best markets: {', '.join(analysis['best_markets'])} | Avoiding: {', '.join(analysis['worst_markets'])}"
            })
        else:
            log(f"  🧠 Not enough trades yet to learn from")

    # Get learning context for this user
    learning_context = user_learning.get(user_id, "")

    # Filter markets based on learning — avoid consistently bad ones
    available = [m for m in MARKETS if m in prices]
    if not available:
        return

    # If we have learning data, prefer good markets
    if learning_context and user_id in user_learning:
        trades = get_trade_history(user_id, limit=30)
        analysis = analyze_trades(trades)
        if analysis and analysis["best_markets"]:
            # 70% chance to pick from best markets if available
            best_available = [m for m in analysis["best_markets"] if m in available]
            if best_available and random.random() < 0.7:
                available = best_available

    market = random.choice(available)
    price = float(prices[market])
    price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"

    log(f"  User {user_id[:8]}... analyzing {market} @ {price_str}")

    market_data = fetch_market_data(market)
    if market_data:
        log(f"  RSI={market_data['rsi']} | 24h={market_data['change_24h_pct']}% | Vol={market_data['volume_ratio']}x")

    # Fetch funding rate and order book
    funding = fetch_funding_rate(market)
    orderbook = fetch_orderbook(market)
    if funding:
        log(f"  Funding={funding['funding_rate']}% ({funding['funding_signal']}) | OB ratio={orderbook['bid_ask_ratio'] if orderbook else 'N/A'}")

    decision = call_claude(market, price_str, risk, leverage, market_data, learning_context, funding, orderbook)

    if decision.get("trade"):
        side = decision.get("side", "long")
        confidence = decision.get("confidence", "medium")
        size_cad = balance * (trade_size_pct / 100)
        pnl, fees, exit_reason = simulate_pnl(size_cad, leverage, confidence, balance)
        won = pnl > 0
        balance = max(0, balance + pnl)
        total_pnl += pnl
        trade_count += 1

        result = "WIN ✓" if won else "LOSS ✗"
        log(f"  {market} {side.upper()} [{result}] Exit: {exit_reason} | P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} CAD (fees: -${fees:.3f}) | Balance: ${balance:.2f} CAD")

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
            "description": f"{decision.get('reason', '')} · Exit: {exit_reason} · P&L: {'+' if pnl >= 0 else ''}${abs(pnl):.2f} CAD · Fees: -${fees:.3f}"
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
    global scan_counter
    log("🤖 LiquidBot server started")
    log(f"   Supabase: {SUPABASE_URL}")
    log(f"   Claude AI: {'ENABLED ✓' if ANTHROPIC_KEY else 'fallback mode'}")
    log(f"   Scan interval: {SCAN_INTERVAL}s")
    log(f"   Trading fee: {TRADING_FEE*100:.2f}% | Slippage: {SLIPPAGE*100:.2f}%")
    log(f"   Stop loss: {STOP_LOSS_PCT*100:.1f}% | Take profit: {TAKE_PROFIT_PCT*100:.1f}% | Hard stop: ${HARD_STOP_BALANCE}")
    log(f"   Self-learning: every {LEARN_EVERY} scans")
    log(f"   Mode: RSI + SMA + Volume + Funding + Order Book + Self Learning 🧠")
    log("")

    consecutive_errors = 0

    while True:
        try:
            scan_counter += 1
            do_learning = scan_counter % LEARN_EVERY == 0
            log(f"--- Scan #{scan_counter} started {'(learning scan) 🧠' if do_learning else ''} ---")

            prices = fetch_prices()
            if not prices:
                log("  No prices, skipping")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    log("  ⚠️ 3 consecutive errors — waiting 60s before retry")
                    time.sleep(60)
                    consecutive_errors = 0
                time.sleep(SCAN_INTERVAL)
                continue

            consecutive_errors = 0
            log(f"  Got prices for {len(prices)} markets")
            users = get_all_users()
            active = [u for u in users if u.get("bot_enabled", True)]
            log(f"  Found {len(active)} active user(s)")

            for user in active:
                try:
                    scan_for_user(user, prices, do_learning)
                except Exception as e:
                    log(f"  ⚠️ Error for user {str(user.get('user_id','?'))[:8]}: {e}")

            log(f"--- Scan complete. Sleeping {SCAN_INTERVAL}s ---\n")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("Bot stopped by user.")
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"⚠️ Unexpected error (#{consecutive_errors}): {e}")
            log("  Restarting in 30s...")
            time.sleep(30)

if __name__ == "__main__":
    main()

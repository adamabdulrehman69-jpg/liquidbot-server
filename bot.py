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
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "180"))  # 3 minutes
CAD_USD = 0.74
START_CAD = 100.0
TRADING_FEE = 0.0004   # 0.04% per trade (realistic Liquid fee)
SLIPPAGE = 0.0005      # 0.05% slippage per trade

MARKETS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "ARB", "DOGE", "BNB", "XRP"]

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

# ---- CLAUDE AI ----
def call_claude(market, price, price_str, risk, leverage):
    if not ANTHROPIC_KEY:
        # Realistic fallback — more conservative than before
        trade_prob = 0.40 if risk == "High" else 0.30 if risk == "Medium" else 0.20
        side = random.choice(["long", "short"])
        return {
            "trade": random.random() < trade_prob,
            "side": side,
            "confidence": "low",
            "reason": f"Fallback logic — no AI key set."
        }
    try:
        prompt = f"""You are a crypto trading bot making real paper trading decisions.
Market: {market}
Current price: {price_str}
Risk level: {risk}
Leverage: {leverage}x

Analyze whether to trade right now based on:
- Is this a good entry point given the current price?
- What direction makes more sense (long = buy, short = sell)?
- How confident are you?

Be conservative — only trade when there is a clear signal. Skip most trades.
Respond ONLY in JSON, no other text:
{{"trade": true/false, "side": "long"/"short", "confidence": "low"/"medium"/"high", "reason": "one sentence explanation"}}"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}]
            },
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        text = r.json()["content"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        log(f"  Claude: trade={result.get('trade')} side={result.get('side')} conf={result.get('confidence')}")
        return result
    except Exception as e:
        log(f"  Claude error: {e}")
        return {"trade": False, "side": "long", "confidence": "low", "reason": "AI error, skipping trade."}

# ---- SIMULATE REALISTIC P&L ----
def simulate_pnl(size_cad, leverage, confidence):
    notional = size_cad * leverage

    # Fee cost (always negative — you always pay fees)
    fee_cost = notional * TRADING_FEE * 2  # entry + exit fee
    slippage_cost = notional * SLIPPAGE

    # Win probability based on confidence
    win_prob = 0.52 if confidence == "high" else 0.48 if confidence == "medium" else 0.44

    won = random.random() < win_prob

    if won:
        # Realistic win: 0.3% to 2% gain on notional
        gross_pnl = notional * random.uniform(0.003, 0.02)
    else:
        # Realistic loss: 0.3% to 1.5% loss on notional
        gross_pnl = -notional * random.uniform(0.003, 0.015)

    # Subtract fees and slippage
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
        log(f"  User {user_id[:8]}... balance too low (${balance:.2f} CAD)")
        return

    available = [m for m in MARKETS if m in prices]
    if not available:
        log(f"  No markets available")
        return

    market = random.choice(available)
    price = float(prices[market])
    price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"

    log(f"  User {user_id[:8]}... analyzing {market} @ {price_str}")

    decision = call_claude(market, price, price_str, risk, leverage)

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
            "user_id": user_id,
            "market": market,
            "side": side,
            "price": price_str,
            "size_cad": round(size_cad, 4),
            "leverage": leverage,
            "pnl_cad": pnl,
            "confidence": confidence,
            "reason": decision.get("reason", "")
        })

        supa_post("alerts", {
            "user_id": user_id,
            "type": "win" if won else "loss",
            "title": f"{market} {side.upper()} — {result}",
            "description": f"{decision.get('reason', '')} · P&L: {'+' if pnl >= 0 else ''}${abs(pnl):.2f} CAD · Fees: -${fees:.3f}"
        })

        if balance / START_CAD < 0.5:
            supa_post("alerts", {
                "user_id": user_id,
                "type": "warn",
                "title": "⚠️ Balance below 50%",
                "description": f"Paper balance at ${balance:.2f} CAD — consider pausing the bot"
            })
            log(f"  ⚠️ Balance warning sent")
    else:
        log(f"  {market} SKIP — {decision.get('reason', 'no signal')}")

    save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk)

# ---- MAIN ----
def main():
    log("🤖 LiquidBot server started")
    log(f"   Supabase: {SUPABASE_URL}")
    log(f"   Claude AI: {'ENABLED ✓' if ANTHROPIC_KEY else 'fallback mode (no key)'}")
    log(f"   Scan interval: {SCAN_INTERVAL}s")
    log(f"   Trading fee: {TRADING_FEE*100:.2f}% | Slippage: {SLIPPAGE*100:.2f}%")
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

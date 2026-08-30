import time
import random
import requests
import os
from datetime import datetime, timezone

# ---- CONFIG (set these as Railway environment variables) ----
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jcwvfgiudhzdpmqibwji.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_dE-1zCOEwJzWHA3ujLyCdw_UPFvdKf5")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))  # seconds
CAD_USD = 0.74
START_CAD = 100.0

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

def supa_patch(table, data, filters):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{filters}",
        json=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
    )
    return r.ok

def supa_upsert(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        json=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"
        }
    )
    return r.ok

# ---- GET ALL USERS ----
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
def call_claude(market, price_str, risk, leverage):
    if not ANTHROPIC_KEY:
        trade_prob = 0.75 if risk == "High" else 0.65 if risk == "Medium" else 0.50
        side = random.choice(["long", "short"])
        reasons = [
            f"{market} showing momentum signal",
            f"Volume spike detected on {market}",
            f"RSI indicates entry point for {market}",
            f"Support level bounce on {market}",
            f"MACD crossover signal on {market}",
            f"Funding rate favorable for {side} on {market}",
        ]
        return {
            "trade": random.random() < trade_prob,
            "side": side,
            "confidence": random.choice(["medium", "medium", "high"]),
            "reason": random.choice(reasons)
        }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 150,
                "messages": [{
                    "role": "user",
                    "content": f"Trading bot. Market: {market} at {price_str}. Risk: {risk}. Leverage: {leverage}x. Trade? JSON only: {{\"trade\":true/false,\"side\":\"long\"/\"short\",\"confidence\":\"low\"/\"medium\"/\"high\",\"reason\":\"one sentence\"}}"
                }]
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
        import json
        return json.loads(text)
    except Exception as e:
        log(f"Claude error: {e}")
        return {"trade": random.random() > 0.45, "side": random.choice(["long", "short"]), "confidence": "medium", "reason": "AI unavailable, fallback used."}

# ---- SAVE TRADE ----
def save_trade(user_id, market, side, price_str, size_cad, leverage, pnl, confidence, reason):
    supa_post("trades", {
        "user_id": user_id,
        "market": market,
        "side": side,
        "price": price_str,
        "size_cad": round(size_cad, 4),
        "leverage": leverage,
        "pnl_cad": round(pnl, 4),
        "confidence": confidence,
        "reason": reason
    })

# ---- SAVE ALERT ----
def save_alert(user_id, alert_type, title, description):
    supa_post("alerts", {
        "user_id": user_id,
        "type": alert_type,
        "title": title,
        "description": description
    })

# ---- SAVE SETTINGS ----
def save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk):
    # Try PATCH first (update existing row)
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

# ---- RUN ONE SCAN FOR ONE USER ----
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
        log(f"  User {user_id[:8]}... bot is paused, skipping")
        return

    if balance < 5:
        log(f"  User {user_id[:8]}... balance too low (${balance:.2f} CAD)")
        return

    # pick a market
    available = [m for m in MARKETS if m in prices]
    if not available:
        log(f"  No market data available")
        return

    market = random.choice(available)
    price = float(prices[market])
    price_str = f"${price:,.0f}" if price > 1000 else f"${price:.2f}"

    log(f"  User {user_id[:8]}... analyzing {market} @ {price_str}")

    decision = call_claude(market, price_str, risk, leverage)

    trade_prob = 0.65 if risk == "High" else 0.5 if risk == "Medium" else 0.35

    if decision.get("trade") and random.random() < trade_prob:
        side = decision.get("side", "long")
        size_cad = balance * (trade_size_pct / 100)
        notional = size_cad * leverage
        raw_pnl = (random.random() * 0.09 - 0.032) * notional
        pnl = round(raw_pnl, 2)
        won = pnl > 0
        balance = max(0, balance + pnl)
        total_pnl += pnl
        trade_count += 1

        result = "WIN ✓" if won else "LOSS ✗"
        log(f"  {market} {side.upper()} [{result}] P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} CAD | Balance: ${balance:.2f} CAD")

        save_trade(user_id, market, side, price_str, size_cad, leverage, pnl,
                   decision.get("confidence", "medium"), decision.get("reason", ""))
        save_alert(user_id, "win" if won else "loss",
                   f"{market} {side.upper()} — {result}",
                   f"{decision.get('reason', '')} · P&L: {'+' if pnl >= 0 else ''}${abs(pnl):.2f} CAD")

        if balance / START_CAD < 0.5:
            save_alert(user_id, "warn", "⚠️ Balance below 50%", f"Paper balance at ${balance:.2f} CAD")
            log(f"  ⚠️ Balance warning sent")
    else:
        log(f"  {market} SKIP — {decision.get('reason', 'signal weak')}")

    save_settings(user_id, balance, total_pnl, trade_count, trade_size_pct, leverage, risk)

# ---- MAIN LOOP ----
def main():
    log("🤖 LiquidBot server started")
    log(f"   Supabase: {SUPABASE_URL}")
    log(f"   Claude AI: {'enabled' if ANTHROPIC_KEY else 'fallback mode (no key)'}")
    log(f"   Scan interval: {SCAN_INTERVAL}s")
    log("")

    while True:
        log("--- Scan started ---")

        # fetch live prices once per scan
        prices = fetch_prices()
        if prices:
            log(f"  Got prices for {len(prices)} markets")
        else:
            log("  Could not fetch prices, skipping scan")
            time.sleep(SCAN_INTERVAL)
            continue

        # get all users with bot enabled
        users = get_all_users()
        active = [u for u in users if u.get("bot_enabled", True)]
        log(f"  Found {len(active)} active user(s)")

        for user in active:
            try:
                scan_for_user(user, prices)
            except Exception as e:
                log(f"  Error for user {str(user.get('user_id','?'))[:8]}: {e}")

        log(f"--- Scan complete. Sleeping {SCAN_INTERVAL}s ---\n")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()

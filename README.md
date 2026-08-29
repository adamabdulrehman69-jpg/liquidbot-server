# LiquidBot Server — 24/7 Trading Bot

This runs on Railway.app for free and trades 24/7 even when your phone is off.

## Deploy to Railway

### Step 1 — Push to GitHub
1. Go to github.com → New repository → name it `liquidbot-server` → Create
2. Upload all files in this folder (bot.py, requirements.txt, Procfile)

### Step 2 — Deploy on Railway
1. Go to railway.app → New Project → Deploy from GitHub repo
2. Select your `liquidbot-server` repo
3. Railway auto-detects Python and deploys it

### Step 3 — Set environment variables
In Railway → your project → Variables, add these:

| Variable | Value |
|---|---|
| SUPABASE_URL | https://jcwvfgiudhzdpmqibwji.supabase.co |
| SUPABASE_KEY | sb_publishable_dE-1zCOEwJzWHA3ujLyCdw_UPFvdKf5 |
| ANTHROPIC_KEY | (your Anthropic API key — optional) |
| SCAN_INTERVAL | 300 |

### Step 4 — Watch it run
Click "View Logs" in Railway — you'll see the bot scanning every 5 minutes.
Open your LiquidBot app — trades and alerts will appear in real time!

## Notes
- Free Railway tier gives $5/month credit — this bot uses ~$1-2/month
- No credit card needed for the free tier
- The bot runs for ALL users who have signed up on your app
- ANTHROPIC_KEY is optional — without it the bot uses fallback logic

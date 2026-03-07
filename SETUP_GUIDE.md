# 🤖 Crypto Portfolio Telegram Bot — Setup Guide

## What this bot does
Every day at **08:00 UTC** it sends you:
- 📊 Your full portfolio value & P&L per coin
- 🔔 Price alerts if any coin moves ±10% in 24h
- 😱 Crypto Fear & Greed index
- 📰 Top 5 hot crypto news headlines

You can also trigger it manually with `/update` anytime.

---

## Step 1 — Create your Telegram Bot (5 mins)

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name, e.g. `My Crypto Tracker`
4. Choose a username ending in `bot`, e.g. `mycryptotracker_bot`
5. BotFather will give you a token like:
   ```
   123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   **Save this — it's your `BOT_TOKEN`**

---

## Step 2 — Get your Chat ID (2 mins)

1. Start a chat with your new bot on Telegram (send `/start`)
2. Open this URL in your browser (replace TOKEN with yours):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Look for `"chat":{"id":` in the response — that number is your **`CHAT_ID`**

---

## Step 3 — Deploy to Railway (free, 10 mins)

1. Go to [railway.app](https://railway.app) and sign up (free tier works)
2. Click **"New Project"** → **"Deploy from GitHub repo"**
   - Or use **"Deploy from local"** and upload this folder
3. Once deployed, go to your project → **Variables** tab
4. Add these two environment variables:
   ```
   BOT_TOKEN   = your token from Step 1
   CHAT_ID     = your chat ID from Step 2
   ```
5. Railway will automatically restart the bot with the new variables

> 💡 **Optional variable:** Set `ALERT_PCT=5` if you want alerts for 5%+ moves (default is 10%)

---

## Step 4 — Test it

Open your Telegram bot and send:
- `/start` — confirms it's alive
- `/update` — gets a full update right now
- `/prices` — live prices only

---

## Files in this package

| File | Purpose |
|------|---------|
| `bot.py` | The main bot code |
| `requirements.txt` | Python dependencies |
| `railway.toml` | Railway deployment config |

---

## Troubleshooting

**Bot doesn't respond?**
- Double-check `BOT_TOKEN` and `CHAT_ID` in Railway variables
- Make sure you sent `/start` to the bot first in Telegram

**Prices showing $0?**
- CoinGecko has rate limits on the free tier. The bot will retry on next run.

**Want to change the daily time?**
- In `bot.py`, find `"08:00"` and change it to your preferred UTC time

---

## Updating your portfolio

When you buy/sell coins, edit `bot.py`:
- `PORTFOLIO` dict — update quantities
- `COST_BASIS` dict — update your total invested per coin

Then redeploy on Railway (it auto-redeploys if connected to GitHub).

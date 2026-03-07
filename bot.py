import os
import json
import asyncio
import logging
from datetime import datetime, time as dtime
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Environment ────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHAT_ID       = os.environ["CHAT_ID"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── Persistent storage (saved to disk so it survives restarts) ─────────────────
MEMORY_FILE = "memory.json"

def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "portfolio": {
            "cosmos":          {"qty": 225.00,    "cost": 1258.24},
            "layerzero":       {"qty": 142.50,    "cost": 185.00},
            "cow-protocol":    {"qty": 1000.00,   "cost": 333.90},
            "pudgy-penguins":  {"qty": 25000.00,  "cost": 537.87},
            "dogecoin":        {"qty": 1750.00,   "cost": 159.90},
            "zksync":          {"qty": 5000.00,   "cost": 289.19},
            "researchcoin":    {"qty": 1000.00,   "cost": 300.00},
            "solana":          {"qty": 1.00,      "cost": 175.00},
            "fuel-network":    {"qty": 60014.00,  "cost": 402.99},
            "across-protocol": {"qty": 2000.00,   "cost": 288.85},
            "aptos":           {"qty": 50.00,     "cost": 193.00},
            "wormhole":        {"qty": 2500.00,   "cost": 51.40},
            "arbius":          {"qty": 100.00,    "cost": 2663.00},
            "solforge":        {"qty": 1000.00,   "cost": 385.18},
            "persistence":     {"qty": 2621.00,   "cost": 28.12},
            "data-lake":       {"qty": 270000.00, "cost": 232.26},
        },
        "symbols": {
            "cosmos": "ATOM", "layerzero": "ZRO", "cow-protocol": "COW",
            "pudgy-penguins": "PENGU", "dogecoin": "DOGE", "zksync": "ZK",
            "researchcoin": "RSC", "solana": "SOL", "fuel-network": "FUEL",
            "across-protocol": "ACX", "aptos": "APT", "wormhole": "W",
            "arbius": "AIUS", "solforge": "SFG", "persistence": "XPRT",
            "data-lake": "LAKE",
        },
        "binance_pairs": {
            "cosmos": "ATOMUSDT", "layerzero": "ZROUSDT", "cow-protocol": "COWUSDT",
            "pudgy-penguins": "PENGUUSDT", "dogecoin": "DOGEUSDT", "zksync": "ZKUSDT",
            "solana": "SOLUSDT", "fuel-network": "FUELUSDT", "aptos": "APTUSDT",
            "wormhole": "WUSDT",
        },
        "coingecko_ids": {
            "researchcoin": "researchcoin", "across-protocol": "across-protocol",
            "arbius": "arbius", "solforge": "solforge",
            "persistence": "persistence", "data-lake": "data-lake",
        },
        "preferences": [],
        "tasks": [],
        "conversation": [],
    }

def save_memory(mem: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)

memory = load_memory()

# ── Price fetching ─────────────────────────────────────────────────────────────

async def get_price_binance(client, coin_id, pair):
    try:
        r = await client.get(
            f"https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": pair}, timeout=10
        )
        d = r.json()
        return coin_id, float(d["lastPrice"]), float(d["priceChangePercent"])
    except Exception as e:
        logger.warning(f"Binance {pair} failed: {e}")
        return coin_id, None, None

async def fetch_all_prices() -> dict:
    prices = {}
    async with httpx.AsyncClient() as client:
        # Binance coins in parallel
        tasks = [
            get_price_binance(client, cid, pair)
            for cid, pair in memory["binance_pairs"].items()
        ]
        for cid, price, chg in await asyncio.gather(*tasks):
            if price:
                prices[cid] = {"price": price, "change_24h": chg}

        # CoinGecko for smaller coins
        try:
            ids = ",".join(memory["coingecko_ids"].keys())
            r = await client.get(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usd&include_24hr_change=true",
                timeout=15
            )
            if r.status_code == 200:
                for cid, data in r.json().items():
                    prices[cid] = {
                        "price": data.get("usd", 0),
                        "change_24h": data.get("usd_24h_change", 0) or 0
                    }
        except Exception as e:
            logger.warning(f"CoinGecko fallback failed: {e}")

    return prices

def build_portfolio_summary(prices: dict) -> str:
    lines = []
    total_value = 0
    total_cost = sum(v["cost"] for v in memory["portfolio"].values())

    for cid, holding in memory["portfolio"].items():
        sym = memory["symbols"].get(cid, cid.upper())
        p = prices.get(cid, {})
        price = p.get("price", 0)
        chg = p.get("change_24h", 0)
        value = price * holding["qty"]
        pnl = value - holding["cost"]
        pnl_pct = (pnl / holding["cost"] * 100) if holding["cost"] else 0
        total_value += value
        arrow = "🟢" if chg >= 0 else "🔴"
        lines.append(
            f"{arrow} *{sym}* ${value:.2f} | {chg:+.1f}% | P&L: ${pnl:+.0f} ({pnl_pct:+.0f}%)"
        )

    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost else 0
    summary = "\n".join(lines)
    summary += f"\n\n💼 *Total: ${total_value:,.0f}* | P&L: ${total_pnl:+,.0f} ({total_pct:+.1f}%)"
    return summary

def portfolio_as_json(prices: dict) -> str:
    data = []
    total_cost = sum(v["cost"] for v in memory["portfolio"].values())
    total_value = 0
    for cid, holding in memory["portfolio"].items():
        sym = memory["symbols"].get(cid, cid.upper())
        p = prices.get(cid, {})
        price = p.get("price", 0)
        chg = p.get("change_24h", 0)
        value = round(price * holding["qty"], 2)
        pnl = round(value - holding["cost"], 2)
        pnl_pct = round((pnl / holding["cost"] * 100) if holding["cost"] else 0, 1)
        total_value += value
        data.append({
            "symbol": sym, "qty": holding["qty"], "price": price,
            "value": value, "cost": holding["cost"],
            "pnl": pnl, "pnl_pct": pnl_pct, "change_24h": round(chg, 2)
        })
    total_pnl = round(total_value - total_cost, 2)
    return json.dumps({
        "holdings": data,
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": total_pnl,
        "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost else 0, 1),
        "preferences": memory["preferences"],
        "tasks": memory["tasks"],
    }, indent=2)

# ── Claude AI Agent with web search ───────────────────────────────────────────

async def ask_claude(user_message: str, portfolio_json: str) -> str:
    global memory

    now = datetime.utcnow().strftime("%a %d %b %Y %H:%M UTC")
    prefs = "\n".join(memory["preferences"]) if memory["preferences"] else "None saved yet."
    tasks = "\n".join(memory["tasks"]) if memory["tasks"] else "None saved yet."

    system = f"""You are a personal crypto AI assistant and portfolio manager for a user on Telegram. Today is {now}.

You have FULL ACCESS to the user's live portfolio data below, their saved preferences, and their task list.

LIVE PORTFOLIO DATA:
{portfolio_json}

USER'S SAVED PREFERENCES:
{prefs}

USER'S TASK LIST:
{tasks}

YOUR CAPABILITIES:
- You have knowledge of crypto markets and can reason about prices and news
- You remember and update the user's preferences and tasks
- You give honest buy/sell/hold opinions based on the portfolio data
- You explain complex crypto concepts simply
- You provide market sentiment and news analysis

IMPORTANT INSTRUCTIONS:
1. Keep responses SHORT and clear — this is Telegram, use *bold* and bullet points
2. Use emojis naturally
3. When the user tells you their preferences (e.g. "I prefer long-term holds", "alert me when SOL drops 5%"), acknowledge it and tell them you've saved it
4. When they give you a task (e.g. "watch BTC this week", "remind me to check DOGE on Friday"), acknowledge and save it
5. Always caveat investment advice as not financial advice
To save a preference, include this exact tag in your response (it won't be shown to user):
[SAVE_PREF: your preference text here]

To save a task, include this exact tag:
[SAVE_TASK: your task text here]

To delete a task when done:
[DELETE_TASK: exact task text]"""

    # Keep conversation history (last 20 messages)
    memory["conversation"].append({"role": "user", "content": user_message})
    if len(memory["conversation"]) > 20:
        memory["conversation"] = memory["conversation"][-20:]

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "system": system,
        "messages": memory["conversation"],
    }

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload
            )
            if r.status_code != 200:
                logger.error(f"Anthropic API error {r.status_code}: {r.text}")
                return f"❌ AI error ({r.status_code}): {r.text[:200]}"
            data = r.json()

        # Extract text from response
        reply = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                reply += block["text"]

        if not reply:
            reply = "I received your message but couldn't generate a response. Please try again."

        # Process hidden commands
        clean_reply = reply

        if "[SAVE_PREF:" in reply:
            import re
            prefs_found = re.findall(r'\[SAVE_PREF: (.+?)\]', reply)
            for pref in prefs_found:
                if pref not in memory["preferences"]:
                    memory["preferences"].append(pref)
            clean_reply = re.sub(r'\[SAVE_PREF: .+?\]', '', clean_reply)

        if "[SAVE_TASK:" in reply:
            import re
            tasks_found = re.findall(r'\[SAVE_TASK: (.+?)\]', reply)
            for task in tasks_found:
                if task not in memory["tasks"]:
                    memory["tasks"].append(task)
            clean_reply = re.sub(r'\[SAVE_TASK: .+?\]', '', clean_reply)

        if "[DELETE_TASK:" in reply:
            import re
            del_tasks = re.findall(r'\[DELETE_TASK: (.+?)\]', reply)
            for task in del_tasks:
                memory["tasks"] = [t for t in memory["tasks"] if t != task]
            clean_reply = re.sub(r'\[DELETE_TASK: .+?\]', '', clean_reply)

        clean_reply = clean_reply.strip()

        # Save assistant reply to conversation history
        memory["conversation"].append({"role": "assistant", "content": clean_reply})
        save_memory(memory)

        return clean_reply

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return f"❌ AI error: {e}"

# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Your Crypto AI Assistant is live!*\n\n"
        "Just talk to me naturally — I know your full portfolio and can:\n\n"
        "• 📊 Analyse your holdings with live prices\n"
        "• 🔍 Search for latest crypto news & market data\n"
        "• 💡 Give buy/sell/hold opinions\n"
        "• 🧠 Remember your preferences & tasks\n"
        "• 📅 Send you a daily morning update\n\n"
        "Commands:\n"
        "/update — get full portfolio snapshot now\n"
        "/memory — see your saved preferences & tasks\n"
        "/clear — reset conversation history\n\n"
        "_Just type anything to start_ 👇",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_update(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching live prices...")
    try:
        prices = await fetch_all_prices()
        date_str = datetime.utcnow().strftime("%a %d %b %Y, %H:%M UTC")
        text = f"📊 *Portfolio Update*\n_{date_str}_\n\n{build_portfolio_summary(prices)}"
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"❌ Error fetching prices: {e}")

async def cmd_memory(update, context: ContextTypes.DEFAULT_TYPE):
    prefs = "\n".join(f"• {p}" for p in memory["preferences"]) or "_None saved_"
    tasks = "\n".join(f"• {t}" for t in memory["tasks"]) or "_None saved_"
    await update.message.reply_text(
        f"🧠 *Your Saved Memory*\n\n"
        f"*Preferences:*\n{prefs}\n\n"
        f"*Tasks:*\n{tasks}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_clear(update, context: ContextTypes.DEFAULT_TYPE):
    memory["conversation"] = []
    save_memory(memory)
    await update.message.reply_text("🧹 Conversation history cleared!")

async def safe_send(msg, text):
    """Try markdown first, fall back to plain text."""
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception:
        try:
            await msg.edit_text(text, disable_web_page_preview=True)
        except Exception as e:
            await msg.edit_text(f"❌ Send error: {e}")

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    msg = await update.message.reply_text("🤔 Thinking...")
    try:
        prices = await fetch_all_prices()
        port_json = portfolio_as_json(prices)
        reply = await ask_claude(user_text, port_json)
        await safe_send(msg, reply)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

# ── Daily scheduled update ─────────────────────────────────────────────────────

async def daily_update(context: ContextTypes.DEFAULT_TYPE):
    try:
        prices = await fetch_all_prices()
        port_json = portfolio_as_json(prices)
        date_str = datetime.utcnow().strftime("%a %d %b %Y")

        # Portfolio snapshot
        summary = build_portfolio_summary(prices)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🌅 *Good morning! Daily Update — {date_str}*\n\n{summary}",
            parse_mode=ParseMode.MARKDOWN
        )

        # AI daily insight with web search
        insight = await ask_claude(
            "Give me my daily portfolio briefing. Which of my coins are moving significantly today? "
            "What should I watch? Any key observations? Keep it punchy — 4-5 bullet points max.",
            port_json
        )
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🧠 *AI Daily Briefing*\n\n{insight}",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        logger.info("Daily update sent.")
    except Exception as e:
        logger.error(f"Daily update failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily update at 08:00 UTC
    app.job_queue.run_daily(
        daily_update,
        time=dtime(hour=8, minute=0),
        name="daily"
    )

    logger.info("🚀 Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()

import os
import asyncio
import logging
import json
from datetime import datetime
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
CHAT_ID         = os.environ["CHAT_ID"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
ALERT_PCT       = float(os.getenv("ALERT_PCT", "10"))

# ── Portfolio data ─────────────────────────────────────────────────────────────
PORTFOLIO = {
    "cosmos":          225.00,
    "layerzero":       142.50,
    "cow-protocol":   1000.00,
    "pudgy-penguins": 25000.00,
    "dogecoin":       1750.00,
    "zksync":         5000.00,
    "researchcoin":   1000.00,
    "solana":            1.00,
    "fuel-network":  60014.00,
    "across-protocol":2000.00,
    "aptos":            50.00,
    "wormhole":       2500.00,
    "arbius":          100.00,
    "solforge":       1000.00,
    "persistence":    2621.00,
    "data-lake":    270000.00,
}

COST_BASIS = {
    "cosmos":         1258.24,
    "layerzero":       185.00,
    "cow-protocol":    333.90,
    "pudgy-penguins":  537.87,
    "dogecoin":        159.90,
    "zksync":          289.19,
    "researchcoin":    300.00,
    "solana":          175.00,
    "fuel-network":    402.99,
    "across-protocol": 288.85,
    "aptos":           193.00,
    "wormhole":         51.40,
    "arbius":         2663.00,
    "solforge":        385.18,
    "persistence":      28.12,
    "data-lake":       232.26,
}

COIN_SYMBOLS = {
    "cosmos": "ATOM", "layerzero": "ZRO", "cow-protocol": "COW",
    "pudgy-penguins": "PENGU", "dogecoin": "DOGE", "zksync": "ZK",
    "researchcoin": "RSC", "solana": "SOL", "fuel-network": "FUEL",
    "across-protocol": "ACX", "aptos": "APT", "wormhole": "W",
    "arbius": "AIUS", "solforge": "SFG", "persistence": "XPRT",
    "data-lake": "LAKE",
}

# ── Conversation memory (per chat session) ─────────────────────────────────────
conversation_history: list[dict] = []
MAX_HISTORY = 20  # keep last 20 messages for context

# ── API helpers ────────────────────────────────────────────────────────────────

async def fetch_prices() -> dict:
    ids = ",".join(PORTFOLIO.keys())
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

async def fetch_fear_greed() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://api.alternative.me/fng/?limit=1")
        r.raise_for_status()
        data = r.json()["data"][0]
        return {"value": data["value"], "label": data["value_classification"]}

async def fetch_news() -> list[dict]:
    url = "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&public=true&kind=news&filter=hot"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            results = r.json().get("results", [])
            return [{"title": item["title"], "url": item["url"]} for item in results[:5]]
    except Exception:
        return []

def build_portfolio_snapshot(prices: dict) -> dict:
    """Build a structured portfolio snapshot for AI context."""
    holdings = []
    total_value = 0.0
    total_cost = sum(COST_BASIS.values())

    for coin_id, qty in PORTFOLIO.items():
        symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
        data = prices.get(coin_id, {})
        price = data.get("usd", 0)
        chg24 = data.get("usd_24h_change", 0) or 0
        value = price * qty
        cost = COST_BASIS.get(coin_id, 0)
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        total_value += value

        holdings.append({
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "value": round(value, 2),
            "cost": cost,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 1),
            "change_24h": round(chg24, 2),
        })

    total_pnl = total_value - total_cost
    return {
        "holdings": holdings,
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost else 0, 1),
    }

# ── Claude AI Agent ────────────────────────────────────────────────────────────

async def ask_claude(user_message: str, portfolio_snapshot: dict = None, news: list = None, fg: dict = None) -> str:
    """Send a message to Claude Sonnet with full portfolio context."""
    global conversation_history

    # Build rich system prompt with live data
    portfolio_json = json.dumps(portfolio_snapshot, indent=2) if portfolio_snapshot else "Not loaded yet"
    news_text = "\n".join([f"- {n['title']}" for n in news]) if news else "Not loaded yet"
    fg_text = f"{fg['label']} ({fg['value']}/100)" if fg else "Not loaded yet"

    system_prompt = f"""You are an expert crypto portfolio AI assistant and financial analyst. You are embedded in a Telegram bot for a user who wants daily guidance, analysis, and smart suggestions about their crypto portfolio.

You have access to the user's LIVE portfolio data right now:

PORTFOLIO SNAPSHOT (live):
{portfolio_json}

MARKET SENTIMENT (Fear & Greed Index): {fg_text}

TOP CRYPTO NEWS RIGHT NOW:
{news_text}

YOUR PERSONALITY & STYLE:
- Be direct, smart, and concise — this is Telegram, not an essay
- Use emojis naturally to make messages readable
- Give real opinions and suggestions, don't just be neutral
- Point out risks clearly but without being alarmist
- When asked for buy/sell suggestions, give a real view (but always remind this isn't financial advice)
- Remember previous messages in the conversation for context
- You can accept daily tasks like "watch for X" or "remind me about Y" and acknowledge them

IMPORTANT: Keep responses under 300 words unless the user asks for detail. Format nicely for Telegram (use *bold*, _italic_ where helpful)."""

    # Add user message to history
    conversation_history.append({"role": "user", "content": user_message})

    # Keep history manageable
    if len(conversation_history) > MAX_HISTORY:
        conversation_history = conversation_history[-MAX_HISTORY:]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": conversation_history,
                }
            )
            response.raise_for_status()
            data = response.json()
            reply = data["content"][0]["text"]

            # Add assistant reply to history
            conversation_history.append({"role": "assistant", "content": reply})
            return reply

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return f"❌ AI agent error: {e}"

# ── Message builders ───────────────────────────────────────────────────────────

def build_portfolio_section(prices: dict) -> tuple[str, float, float]:
    lines = []
    total_value = 0.0
    total_cost = sum(COST_BASIS.values())
    gainers, losers = [], []

    for coin_id, qty in PORTFOLIO.items():
        symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
        data = prices.get(coin_id, {})
        price = data.get("usd", 0)
        chg24 = data.get("usd_24h_change", 0) or 0
        value = price * qty
        cost = COST_BASIS.get(coin_id, 0)
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        total_value += value

        arrow = "🟢" if chg24 >= 0 else "🔴"
        lines.append(
            f"{arrow} *{symbol}* — ${value:.2f}  _{chg24:+.2f}% 24h_\n"
            f"   P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
        )

        if chg24 >= ALERT_PCT:
            gainers.append(f"🚀 *{symbol}* surged {chg24:+.1f}% in 24h!")
        elif chg24 <= -ALERT_PCT:
            losers.append(f"⚠️ *{symbol}* dropped {chg24:.1f}% in 24h!")

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
    section = "\n".join(lines)
    section += (
        f"\n\n💼 *Portfolio Total: ${total_value:,.2f}*\n"
        f"📊 Overall P&L: ${total_pnl:+,.2f} ({total_pnl_pct:+.1f}%)"
    )
    if gainers or losers:
        section += "\n\n🔔 *Price Alerts*\n" + "\n".join(gainers + losers)

    return section, total_value, total_pnl


def build_daily_message(prices: dict, fg: dict, news: list[dict]) -> str:
    date_str = datetime.utcnow().strftime("%a %d %b %Y, %H:%M UTC")
    fg_emoji = {"Extreme Fear": "😱", "Fear": "😰", "Neutral": "😐",
                "Greed": "😏", "Extreme Greed": "🤑"}.get(fg["label"], "📊")
    portfolio_text, _, _ = build_portfolio_section(prices)
    news_lines = "\n\n📰 *Top Crypto News*\n" + "\n".join(
        f"• [{item['title']}]({item['url']})" for item in news
    ) if news else "\n\n📰 _No news available._"

    return (
        f"🌅 *Daily Crypto Update*\n_{date_str}_\n\n"
        f"😨 *Market Sentiment*: {fg_emoji} {fg['label']} ({fg['value']}/100)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Your Portfolio*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{portfolio_text}"
        f"{news_lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 _Chat with your AI agent anytime — just type a message or use /ask_\n"
        f"_Next update tomorrow_ ✌️"
    )

# ── Bot command handlers ───────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Crypto Portfolio AI Bot is live!*\n\n"
        "*Commands:*\n"
        "/update — full portfolio update now\n"
        "/prices — live prices only\n"
        "/ask [question] — ask the AI agent\n"
        "/clear — clear AI conversation history\n"
        "/help — show this message\n\n"
        "💬 *Or just type anything* — the AI agent will respond!\n\n"
        "_Powered by Claude Sonnet_ 🤖",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_update(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching live data...")
    try:
        prices, fg, news = await asyncio.gather(
            fetch_prices(), fetch_fear_greed(), fetch_news()
        )
        text = build_daily_message(prices, fg, news)
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def cmd_prices(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching prices...")
    try:
        prices = await fetch_prices()
        lines = []
        for coin_id, qty in PORTFOLIO.items():
            symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
            data = prices.get(coin_id, {})
            price = data.get("usd", 0)
            chg24 = data.get("usd_24h_change", 0) or 0
            arrow = "🟢" if chg24 >= 0 else "🔴"
            lines.append(f"{arrow} *{symbol}*: ${price} ({chg24:+.2f}%)")
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def cmd_ask(update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ask command."""
    question = " ".join(context.args) if context.args else ""
    if not question:
        await update.message.reply_text(
            "💬 Usage: `/ask what should I do with my DOGE?`\n"
            "Or just type your message directly!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    await handle_ai_message(update, context, question)

async def cmd_clear(update, context: ContextTypes.DEFAULT_TYPE):
    """Clear conversation history."""
    global conversation_history
    conversation_history = []
    await update.message.reply_text("🧹 Conversation history cleared! Starting fresh.")

async def handle_ai_message(update, context: ContextTypes.DEFAULT_TYPE, override_text: str = None):
    """Handle any text message with the AI agent."""
    user_text = override_text or update.message.text

    # Show typing indicator
    msg = await update.message.reply_text("🤖 _Thinking..._", parse_mode=ParseMode.MARKDOWN)

    try:
        # Fetch live data for context
        prices, fg, news = await asyncio.gather(
            fetch_prices(), fetch_fear_greed(), fetch_news()
        )
        snapshot = build_portfolio_snapshot(prices)
        reply = await ask_claude(user_text, snapshot, news, fg)

        await msg.edit_text(reply, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

# ── Scheduled daily job ────────────────────────────────────────────────────────

async def send_daily_update(context: ContextTypes.DEFAULT_TYPE):
    try:
        prices, fg, news = await asyncio.gather(
            fetch_prices(), fetch_fear_greed(), fetch_news()
        )
        text = build_daily_message(prices, fg, news)
        await context.bot.send_message(
            chat_id=CHAT_ID, text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

        # Also send an AI-generated daily insight
        snapshot = build_portfolio_snapshot(prices)
        insight = await ask_claude(
            "Give me a brief daily insight about my portfolio today. What should I focus on? "
            "Any coins to watch? Keep it to 3-4 bullet points max.",
            snapshot, news, fg
        )
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🧠 *AI Daily Insight*\n\n{insight}",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info("Daily update + AI insight sent.")
    except Exception as e:
        logger.error(f"Failed to send daily update: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("ask",    cmd_ask))
    app.add_handler(CommandHandler("clear",  cmd_clear))

    # Handle ALL regular text messages with AI agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_message))

    # Schedule daily update at 08:00 UTC
    job_queue = app.job_queue
    job_queue.run_daily(
        send_daily_update,
        time=datetime.strptime("08:00", "%H:%M").time(),
        name="daily_update"
    )

    logger.info("AI-powered bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()

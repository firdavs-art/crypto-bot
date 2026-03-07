"""
Crypto + Stock Portfolio Telegram Bot
- Daily 7am UK briefing: geopolitical, macro, portfolio news
- /prices command: live prices with P&L
- AI Agent: powered by Groq (free) — advises on news, portfolio, buy/sell
"""

import os, json, asyncio, logging, re
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import httpx
import feedparser
import yfinance as yf
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = os.environ["CHAT_ID"]
GROQ_KEY   = os.environ["GROQ_API_KEY"]
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
UK_TZ      = ZoneInfo("Europe/London")

# ── Portfolio ──────────────────────────────────────────────────────────────────
CRYPTO = {
    "ATOM":  {"name": "Cosmos",           "qty": 225.00,    "buy": 5.59},
    "ZRO":   {"name": "LayerZero",        "qty": 142.50,    "buy": 1.29},
    "COW":   {"name": "CoW Protocol",     "qty": 1000.00,   "buy": 0.3339},
    "PENGU": {"name": "Pudgy Penguins",   "qty": 25000.00,  "buy": 0.021514},
    "DOGE":  {"name": "Dogecoin",         "qty": 1750.00,   "buy": 0.091375},
    "ZK":    {"name": "ZKsync",           "qty": 5000.00,   "buy": 0.057838},
    "RSC":   {"name": "ResearchCoin",     "qty": 1000.00,   "buy": 0.30},
    "SOL":   {"name": "Solana",           "qty": 1.00,      "buy": 175.00},
    "FUEL":  {"name": "Fuel Network",     "qty": 60014.00,  "buy": 0.006715},
    "ACX":   {"name": "Across Protocol",  "qty": 2000.00,   "buy": 0.1444},
    "APT":   {"name": "Aptos",            "qty": 50.00,     "buy": 3.86},
    "W":     {"name": "Wormhole",         "qty": 2500.00,   "buy": 0.02056},
    "AIUS":  {"name": "Arbius",           "qty": 100.00,    "buy": 26.63},
    "SFG":   {"name": "SolForge Fusion",  "qty": 1000.00,   "buy": 0.3851},
    "XPRT":  {"name": "Persistence",      "qty": 2621.00,   "buy": 0.01073},
    "LAKE":  {"name": "Data Lake",        "qty": 270000.00, "buy": 0.00086022},
}

STOCK = {
    "BTDR": {"name": "Bitdeer Technologies", "qty": 21.5, "buy": 9.45}
}

# ── Conversation history ───────────────────────────────────────────────────────
history: list[dict] = []
MAX_HISTORY = 30

# ── RSS News Feeds ─────────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("Reuters World",   "https://feeds.reuters.com/reuters/worldNews"),
    ("Reuters Business","https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters US",      "https://feeds.reuters.com/Reuters/domesticNews"),
    ("CNBC Economy",    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("BBC World",       "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("FT Markets",      "https://www.ft.com/rss/home/us"),
]

CRYPTO_FEEDS = [
    ("CoinDesk",        "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph",   "https://cointelegraph.com/rss"),
    ("Decrypt",         "https://decrypt.co/feed"),
]

def fetch_rss(feeds: list, limit_per_feed=5) -> list[str]:
    """Fetch headlines from RSS feeds."""
    headlines = []
    for name, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit_per_feed]:
                title = entry.get("title", "").strip()
                if title:
                    headlines.append(f"[{name}] {title}")
        except Exception as e:
            logger.warning(f"RSS {name} failed: {e}")
    return headlines

# ── Price Fetching ─────────────────────────────────────────────────────────────

async def fetch_crypto_prices() -> dict:
    """Fetch crypto prices from CryptoCompare — free, reliable."""
    symbols = ",".join(CRYPTO.keys())
    url = f"https://min-api.cryptocompare.com/data/pricemultifull?fsyms={symbols}&tsyms=USD"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            r.raise_for_status()
            raw = r.json().get("RAW", {})
            return {
                sym: {
                    "price": raw[sym]["USD"]["PRICE"],
                    "change24h": raw[sym]["USD"]["CHANGEPCT24HOUR"],
                }
                for sym in CRYPTO if sym in raw and "USD" in raw.get(sym, {})
            }
    except Exception as e:
        logger.error(f"Crypto price fetch failed: {e}")
        return {}

def fetch_stock_price(ticker: str) -> dict:
    """Fetch stock price using yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = info.last_price
        prev  = info.previous_close
        change = ((price - prev) / prev * 100) if prev else 0
        return {"price": round(price, 2), "change24h": round(change, 2)}
    except Exception as e:
        logger.error(f"Stock price fetch failed for {ticker}: {e}")
        return {}

# ── Portfolio Builders ─────────────────────────────────────────────────────────

def build_prices_message(crypto_prices: dict, stock_prices: dict) -> str:
    now = datetime.now(UK_TZ).strftime("%a %d %b %Y, %H:%M %Z")
    lines = [f"📊 *Live Portfolio — {now}*\n"]

    total_value = 0
    total_cost  = 0

    lines.append("*🪙 Crypto*")
    for sym, h in CRYPTO.items():
        p = crypto_prices.get(sym, {})
        price  = p.get("price", 0)
        chg    = p.get("change24h", 0)
        value  = price * h["qty"]
        cost   = h["buy"] * h["qty"]
        pnl    = value - cost
        pnl_p  = (pnl / cost * 100) if cost else 0
        total_value += value
        total_cost  += cost
        arrow = "🟢" if chg >= 0 else "🔴"
        if price > 0:
            lines.append(
                f"{arrow} *{sym}* ${price:.4f} ({chg:+.1f}%)\n"
                f"   Val: ${value:.0f} | P&L: ${pnl:+.0f} ({pnl_p:+.0f}%)"
            )
        else:
            lines.append(f"⚪ *{sym}* — price unavailable")

    lines.append("\n*📈 Stocks*")
    for sym, h in STOCK.items():
        p = stock_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        value = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl   = value - cost
        pnl_p = (pnl / cost * 100) if cost else 0
        total_value += value
        total_cost  += cost
        arrow = "🟢" if chg >= 0 else "🔴"
        if price > 0:
            lines.append(
                f"{arrow} *{sym}* ${price:.2f} ({chg:+.1f}%)\n"
                f"   Val: ${value:.0f} | P&L: ${pnl:+.0f} ({pnl_p:+.0f}%)"
            )
        else:
            lines.append(f"⚪ *{sym}* — price unavailable")

    total_pnl  = total_value - total_cost
    total_pnl_p = (total_pnl / total_cost * 100) if total_cost else 0
    lines.append(
        f"\n━━━━━━━━━━━━━━━━\n"
        f"💼 *Total Value: ${total_value:,.0f}*\n"
        f"💰 *Total P&L: ${total_pnl:+,.0f} ({total_pnl_p:+.1f}%)*"
    )
    return "\n".join(lines)

def portfolio_for_ai(crypto_prices: dict, stock_prices: dict) -> str:
    """Compact portfolio summary for AI context."""
    lines = ["PORTFOLIO SNAPSHOT:"]
    for sym, h in CRYPTO.items():
        p     = crypto_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        value = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl_p = ((value - cost) / cost * 100) if cost else 0
        lines.append(f"{sym}: price=${price:.6g} chg={chg:+.1f}% value=${value:.0f} pnl={pnl_p:+.0f}%")
    for sym, h in STOCK.items():
        p     = stock_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        value = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl_p = ((value - cost) / cost * 100) if cost else 0
        lines.append(f"{sym} (stock): price=${price:.2f} chg={chg:+.1f}% value=${value:.0f} pnl={pnl_p:+.0f}%")
    return "\n".join(lines)

# ── Groq AI ────────────────────────────────────────────────────────────────────

def build_system_prompt(crypto_prices: dict, stock_prices: dict) -> str:
    now = datetime.now(UK_TZ).strftime("%A %d %B %Y, %H:%M %Z")
    port = portfolio_for_ai(crypto_prices, stock_prices)
    return f"""You are a sharp, expert crypto and financial AI assistant embedded in a personal Telegram bot.

TODAY: {now}

{port}

USER'S HOLDINGS:
Crypto: ATOM(225), ZRO(142.5), COW(1000), PENGU(25000), DOGE(1750), ZK(5000), RSC(1000), SOL(1), FUEL(60014), ACX(2000), APT(50), W(2500), AIUS(100), SFG(1000), XPRT(2621), LAKE(270000)
Stock: BTDR Bitdeer Technologies (21.5 shares, bought at $9.45)

YOUR ROLE:
- Give honest, direct investment opinions on the portfolio
- Advise on buy/sell/hold decisions with reasoning
- Discuss crypto and macro news intelligently
- Remember context from earlier in this conversation

STYLE:
- Telegram formatting: use *bold*, keep responses concise
- Use emojis naturally but not excessively  
- Be direct — no waffle, no disclaimers unless truly needed
- When asked for an opinion, give one — don't just list pros and cons endlessly"""

async def ask_groq(user_message: str, crypto_prices: dict, stock_prices: dict) -> str:
    global history

    system = build_system_prompt(crypto_prices, stock_prices)
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system}] + history,
        "max_tokens": 1024,
        "temperature": 0.7,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code != 200:
                logger.error(f"Groq error {r.status_code}: {r.text[:300]}")
                return f"❌ AI error ({r.status_code}). Please try again."
            reply = r.json()["choices"][0]["message"]["content"]
            history.append({"role": "assistant", "content": reply})
            return reply
    except Exception as e:
        logger.error(f"Groq request failed: {e}")
        return f"❌ AI request failed: {e}"

async def send_safe(context_or_msg, text: str, chat_id=None):
    """Send message, falling back to plain text if markdown fails."""
    try:
        if chat_id:
            await context_or_msg.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        else:
            await context_or_msg.edit_text(
                text, parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
    except Exception:
        try:
            clean = re.sub(r'[*_`\[\]]', '', text)
            if chat_id:
                await context_or_msg.bot.send_message(
                    chat_id=chat_id, text=clean,
                    disable_web_page_preview=True
                )
            else:
                await context_or_msg.edit_text(clean, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"send_safe failed: {e}")

# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Your Personal Finance AI Assistant*\n\n"
        "I monitor your crypto & stock portfolio and keep you informed.\n\n"
        "*Commands:*\n"
        "📊 /prices — live portfolio with P&L\n"
        "🆘 /help — show this menu\n\n"
        "💬 *Or just talk to me* — ask anything about your portfolio, markets, news, buy/sell advice.\n\n"
        "_Daily briefing arrives every morning at 7:00am UK time_ ⏰"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_prices(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching live prices...")
    try:
        crypto_prices = await fetch_crypto_prices()
        stock_prices  = {sym: fetch_stock_price(sym) for sym in STOCK}
        text = build_prices_message(crypto_prices, stock_prices)
        await send_safe(msg, text)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def cmd_clear(update, context: ContextTypes.DEFAULT_TYPE):
    global history
    history = []
    await update.message.reply_text("🧹 Conversation cleared!")

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    """AI agent handles all regular messages."""
    user_text = update.message.text
    msg = await update.message.reply_text("🤔 Thinking...")
    try:
        crypto_prices = await fetch_crypto_prices()
        stock_prices  = {sym: fetch_stock_price(sym) for sym in STOCK}
        reply = await ask_groq(user_text, crypto_prices, stock_prices)
        await send_safe(msg, reply)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

# ── Daily Morning Briefing ─────────────────────────────────────────────────────

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    """7am UK daily briefing: news + portfolio + AI insight."""
    logger.info("Sending morning briefing...")
    now = datetime.now(UK_TZ).strftime("%A %d %B %Y")

    # 1. Fetch prices
    try:
        crypto_prices = await fetch_crypto_prices()
        stock_prices  = {sym: fetch_stock_price(sym) for sym in STOCK}
    except Exception as e:
        logger.error(f"Price fetch in briefing failed: {e}")
        crypto_prices, stock_prices = {}, {}

    # 2. Fetch news headlines
    world_headlines  = fetch_rss(NEWS_FEEDS,   limit_per_feed=4)
    crypto_headlines = fetch_rss(CRYPTO_FEEDS, limit_per_feed=3)

    # 3. Build briefing prompt
    world_text  = "\n".join(world_headlines[:20])  or "No headlines available"
    crypto_text = "\n".join(crypto_headlines[:10]) or "No crypto headlines available"
    port_text   = portfolio_for_ai(crypto_prices, stock_prices)

    briefing_prompt = f"""Generate my daily morning briefing for {now}. Be sharp and concise.

WORLD/MACRO HEADLINES (select only truly important ones):
{world_text}

CRYPTO HEADLINES:
{crypto_text}

{port_text}

Structure the briefing EXACTLY like this:

🌍 *GEOPOLITICAL & MACRO*
(2-3 bullet points — only truly significant US/EU/Russia/China events, wars, major policy shifts. Skip fluff.)

📊 *MACRO ECONOMY*
(2-3 bullet points — CPI, inflation, Fed/ECB decisions, major economic data. Only if relevant today.)

🪙 *CRYPTO & PORTFOLIO NEWS*
(2-3 bullet points — relevant crypto news, especially for coins I hold. BTDR/Bitdeer news if any.)

📈 *PORTFOLIO SNAPSHOT*
(3-4 biggest movers today with % change. Total portfolio value.)

💡 *AI INSIGHT*
(1-2 sentences — your single most important observation or action point for today.)

Keep the whole briefing under 400 words. Only include a section if there's genuinely important news for it."""

    try:
        reply = await ask_groq(briefing_prompt, crypto_prices, stock_prices)
        header = f"🌅 *Morning Briefing — {now}*\n\n"
        await send_safe(context, header + reply, chat_id=CHAT_ID)
    except Exception as e:
        logger.error(f"Morning briefing failed: {e}")
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ Morning briefing failed: {e}"
        )

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily briefing at 7:00am UK time
    app.job_queue.run_daily(
        morning_briefing,
        time=dtime(hour=7, minute=0, tzinfo=UK_TZ),
        name="morning_briefing"
    )

    logger.info("🚀 Bot is running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

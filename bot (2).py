"""
Personal Finance AI Telegram Bot
- Daily 7am UK briefing: geopolitical, macro, crypto & portfolio news
- /prices  — live portfolio with P&L
- /brief   — trigger morning briefing on demand
- /summary — AI portfolio summary & honest analysis
- /clear   — reset conversation
- AI Agent (Groq/Llama) — full portfolio context, buy/sell advice
"""

import os, asyncio, logging, re
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import httpx
import feedparser
import yfinance as yf
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = os.environ["CHAT_ID"]
GROQ_KEY   = os.environ["GROQ_API_KEY"]
GROQ_URL        = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_CHAT = "llama-3.1-8b-instant"     # 20,000 TPM — regular chat (fast, rarely rate limited)
GROQ_MODEL_BIG  = "llama-3.3-70b-versatile"  # 6,000 TPM  — briefings only
UK_TZ           = ZoneInfo("Europe/London")

# ── Price cache (avoids refetching on every single message) ───────────────────
import time as _time
_price_cache:   dict  = {"crypto": {}, "stock": {}}
_price_cache_ts: float = 0.0
CACHE_TTL = 90  # seconds

# ── Portfolio ──────────────────────────────────────────────────────────────────

# Mainstream coins — fetched from CryptoCompare (fast, reliable, no rate limits)
CRYPTO_CC = {
    "ATOM":  {"name": "Cosmos",          "qty": 225.00,   "buy": 5.59},
    "ZRO":   {"name": "LayerZero",       "qty": 142.50,   "buy": 1.29},
    "COW":   {"name": "CoW Protocol",    "qty": 1000.00,  "buy": 0.3339},
    "PENGU": {"name": "Pudgy Penguins",  "qty": 25000.00, "buy": 0.021514},
    "DOGE":  {"name": "Dogecoin",        "qty": 1750.00,  "buy": 0.091375},
    "ZK":    {"name": "ZKsync",          "qty": 5000.00,  "buy": 0.057838},
    "RSC":   {"name": "ResearchCoin",    "qty": 1000.00,  "buy": 0.30},
    "SOL":   {"name": "Solana",          "qty": 1.00,     "buy": 175.00},
    "FUEL":  {"name": "Fuel Network",    "qty": 60014.00, "buy": 0.006715},
    "ACX":   {"name": "Across Protocol", "qty": 2000.00,  "buy": 0.1444},
    "APT":   {"name": "Aptos",           "qty": 50.00,    "buy": 3.86},
    "W":     {"name": "Wormhole",        "qty": 2500.00,  "buy": 0.02056},
    "XPRT":  {"name": "Persistence",     "qty": 2621.00,  "buy": 0.01073},
}

# Small DEX-only coins — fetched from CoinGecko (correct IDs verified)
CRYPTO_GECKO = {
    "AIUS": {"name": "Arbius",          "qty": 100.00,    "buy": 26.63,      "gecko_id": "arbius"},
    "SFG":  {"name": "SolForge Fusion", "qty": 1000.00,   "buy": 0.3851,     "gecko_id": "solforge-fusion"},
    "LAKE": {"name": "Data Lake",       "qty": 270000.00, "buy": 0.00086022, "gecko_id": "data-lake"},
}

# Stocks
STOCK = {
    "BTDR": {"name": "Bitdeer Technologies", "qty": 21.5, "buy": 9.45}
}

# Combined view (without gecko_id field)
CRYPTO = {
    **CRYPTO_CC,
    **{k: {f: v[f] for f in ("name","qty","buy")} for k, v in CRYPTO_GECKO.items()}
}

# ── Conversation history ───────────────────────────────────────────────────────
history: list[dict] = []
MAX_HISTORY = 30

# ── RSS News Feeds ─────────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("Reuters World",    "https://feeds.reuters.com/reuters/worldNews"),
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters US",       "https://feeds.reuters.com/Reuters/domesticNews"),
    ("CNBC Economy",     "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("BBC World",        "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("FT Markets",       "https://www.ft.com/rss/home/us"),
]
CRYPTO_FEEDS = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
]

def fetch_rss(feeds: list, limit_per_feed: int = 5) -> list[str]:
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

async def fetch_cryptocompare() -> dict:
    """CryptoCompare: all 13 mainstream coins in one request."""
    fsyms = ",".join(CRYPTO_CC.keys())
    url = f"https://min-api.cryptocompare.com/data/pricemultifull?fsyms={fsyms}&tsyms=USD"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            r.raise_for_status()
            raw = r.json().get("RAW", {})
            result = {}
            for sym in CRYPTO_CC:
                if sym in raw and "USD" in raw[sym]:
                    result[sym] = {
                        "price":     raw[sym]["USD"]["PRICE"],
                        "change24h": raw[sym]["USD"]["CHANGEPCT24HOUR"],
                    }
            logger.info(f"CryptoCompare: {len(result)}/{len(CRYPTO_CC)} coins")
            return result
    except Exception as e:
        logger.error(f"CryptoCompare failed: {e}")
        return {}

async def fetch_coingecko_small() -> dict:
    """CoinGecko: only AIUS, SFG, LAKE (3 coins — minimal rate limit risk)."""
    ids = ",".join(v["gecko_id"] for v in CRYPTO_GECKO.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            if r.status_code == 429:
                logger.warning("CoinGecko rate limited — AIUS/SFG/LAKE unavailable")
                return {}
            r.raise_for_status()
            data = r.json()
            gecko_to_sym = {v["gecko_id"]: k for k, v in CRYPTO_GECKO.items()}
            result = {}
            for gecko_id, vals in data.items():
                sym = gecko_to_sym.get(gecko_id)
                if sym:
                    result[sym] = {
                        "price":     vals.get("usd", 0) or 0,
                        "change24h": vals.get("usd_24h_change", 0) or 0,
                    }
            logger.info(f"CoinGecko: {len(result)}/3 small coins")
            return result
    except Exception as e:
        logger.error(f"CoinGecko failed: {e}")
        return {}

async def fetch_all_prices(force: bool = False) -> tuple[dict, dict]:
    """Fetch prices — uses cache to avoid hammering APIs on every message."""
    global _price_cache, _price_cache_ts
    now = _time.monotonic()
    if not force and (now - _price_cache_ts) < CACHE_TTL and _price_cache["crypto"]:
        return _price_cache["crypto"], _price_cache["stock"]
    cc, gecko = await asyncio.gather(fetch_cryptocompare(), fetch_coingecko_small())
    crypto_prices = {**cc, **gecko}
    stock_prices  = {sym: fetch_stock_price(sym) for sym in STOCK}
    _price_cache["crypto"] = crypto_prices
    _price_cache["stock"]  = stock_prices
    _price_cache_ts = now
    return crypto_prices, stock_prices

def fetch_stock_price(ticker: str) -> dict:
    try:
        info  = yf.Ticker(ticker).fast_info
        price = info.last_price
        prev  = info.previous_close
        chg   = ((price - prev) / prev * 100) if prev else 0
        return {"price": round(price, 2), "change24h": round(chg, 2)}
    except Exception as e:
        logger.error(f"Stock price failed for {ticker}: {e}")
        return {}

# ── Message Builders ───────────────────────────────────────────────────────────

def build_prices_message(crypto_prices: dict, stock_prices: dict) -> str:
    now = datetime.now(UK_TZ).strftime("%a %d %b %Y, %H:%M %Z")
    lines = [f"📊 *Live Portfolio — {now}*\n"]
    total_value = total_cost = 0

    lines.append("*🪙 Crypto*")
    for sym, h in CRYPTO.items():
        p     = crypto_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        val   = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl   = val - cost
        pnl_p = (pnl / cost * 100) if cost else 0
        total_value += val
        total_cost  += cost
        if price > 0:
            arrow = "🟢" if chg >= 0 else "🔴"
            lines.append(
                f"{arrow} *{sym}* ${price:.4g} ({chg:+.1f}%)\n"
                f"   Val: ${val:,.0f} | P&L: ${pnl:+,.0f} ({pnl_p:+.0f}%)"
            )
        else:
            lines.append(f"⚪ *{sym}* — unavailable")

    lines.append("\n*📈 Stocks*")
    for sym, h in STOCK.items():
        p     = stock_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        val   = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl   = val - cost
        pnl_p = (pnl / cost * 100) if cost else 0
        total_value += val
        total_cost  += cost
        if price > 0:
            arrow = "🟢" if chg >= 0 else "🔴"
            lines.append(
                f"{arrow} *{sym}* ${price:.2f} ({chg:+.1f}%)\n"
                f"   Val: ${val:,.0f} | P&L: ${pnl:+,.0f} ({pnl_p:+.0f}%)"
            )
        else:
            lines.append(f"⚪ *{sym}* — unavailable")

    total_pnl   = total_value - total_cost
    total_pnl_p = (total_pnl / total_cost * 100) if total_cost else 0
    lines.append(
        f"\n━━━━━━━━━━━━━━━━\n"
        f"💼 *Total Value: ${total_value:,.0f}*\n"
        f"💰 *Total P&L: ${total_pnl:+,.0f} ({total_pnl_p:+.1f}%)*"
    )
    return "\n".join(lines)

def portfolio_for_ai(crypto_prices: dict, stock_prices: dict) -> str:
    """Compact portfolio context injected into every AI prompt."""
    lines = ["LIVE PORTFOLIO:"]
    total_value = total_cost = 0
    for sym, h in CRYPTO.items():
        p     = crypto_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        val   = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl_p = ((val - cost) / cost * 100) if cost else 0
        total_value += val
        total_cost  += cost
        lines.append(f"  {sym}: ${price:.5g} | {chg:+.1f}%/24h | val=${val:.0f} | pnl={pnl_p:+.0f}%")
    for sym, h in STOCK.items():
        p     = stock_prices.get(sym, {})
        price = p.get("price", 0)
        chg   = p.get("change24h", 0)
        val   = price * h["qty"]
        cost  = h["buy"] * h["qty"]
        pnl_p = ((val - cost) / cost * 100) if cost else 0
        total_value += val
        total_cost  += cost
        lines.append(f"  {sym} [STOCK]: ${price:.2f} | {chg:+.1f}%/24h | val=${val:.0f} | pnl={pnl_p:+.0f}%")
    total_pnl   = total_value - total_cost
    total_pnl_p = (total_pnl / total_cost * 100) if total_cost else 0
    lines.append(f"TOTAL: val=${total_value:,.0f} | pnl=${total_pnl:+,.0f} ({total_pnl_p:+.1f}%)")
    return "\n".join(lines)

def get_top_movers(crypto_prices: dict, stock_prices: dict, n: int = 4) -> str:
    all_prices = {**crypto_prices, **stock_prices}
    movers = sorted(all_prices.items(), key=lambda x: abs(x[1].get("change24h", 0)), reverse=True)
    parts = []
    for sym, p in movers[:n]:
        chg = p.get("change24h", 0)
        icon = "🚀" if chg > 5 else "🟢" if chg >= 0 else ("💥" if chg < -5 else "🔴")
        parts.append(f"{icon} {sym} {chg:+.1f}%")
    return "  ".join(parts)

# ── Groq AI ────────────────────────────────────────────────────────────────────

def build_system(crypto_prices: dict, stock_prices: dict) -> str:
    now  = datetime.now(UK_TZ).strftime("%A %d %B %Y, %H:%M %Z")
    port = portfolio_for_ai(crypto_prices, stock_prices)
    return f"""You are a sharp personal finance AI inside a Telegram bot. No fluff.

DATE/TIME: {now}

{port}

RULES:
- Give direct, honest opinions on buy/sell/hold — don't just list pros and cons forever
- Use *bold* for key points, keep replies concise (Telegram, not an essay)
- You know the portfolio above in real time — reference it naturally
- No need to add disclaimers to every single message"""

async def ask_groq(user_msg: str, crypto_prices: dict, stock_prices: dict, use_big_model: bool = False) -> str:
    """
    use_big_model=False → llama-3.1-8b-instant (20k TPM, for all chat)
    use_big_model=True  → llama-3.3-70b-versatile (6k TPM, only for briefings)
    """
    global history
    model = GROQ_MODEL_BIG if use_big_model else GROQ_MODEL_CHAT
    history.append({"role": "user", "content": user_msg})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    payload = {
        "model":       model,
        "messages":    [{"role": "system", "content": build_system(crypto_prices, stock_prices)}] + history,
        "max_tokens":  800,
        "temperature": 0.7,
    }

    wait_times = [5, 15, 30]
    for attempt, wait in enumerate(wait_times, 1):
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code == 429:
                    retry_after = int(r.headers.get("retry-after", wait))
                    logger.warning(f"Groq 429 — waiting {retry_after}s (attempt {attempt}/3, model={model})")
                    await asyncio.sleep(retry_after)
                    continue
                if r.status_code != 200:
                    logger.error(f"Groq {r.status_code}: {r.text[:200]}")
                    return f"❌ AI error ({r.status_code}) — try again in a moment."
                reply = r.json()["choices"][0]["message"]["content"]
                history.append({"role": "assistant", "content": reply})
                return reply
        except Exception as e:
            logger.error(f"Groq attempt {attempt} failed: {e}")
            await asyncio.sleep(wait)

    return "❌ Groq is busy — please try again in a minute."

# ── Safe Send (markdown → plain fallback) ─────────────────────────────────────

async def send_safe(target, text: str, chat_id: str = None):
    strip = lambda t: re.sub(r'[*_`\[\]]', '', t)
    for md, txt in [(True, text), (False, strip(text))]:
        try:
            kw = {"disable_web_page_preview": True}
            if md:
                kw["parse_mode"] = ParseMode.MARKDOWN
            if chat_id:
                await target.bot.send_message(chat_id=chat_id, text=txt, **kw)
            else:
                await target.edit_text(txt, **kw)
            return
        except Exception as e:
            if not md:
                logger.error(f"send_safe failed completely: {e}")

# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Your Personal Finance AI*\n\n"
        "*Commands:*\n"
        "📊 /prices — live portfolio & P&L\n"
        "🌅 /brief — morning briefing on demand\n"
        "📋 /summary — honest portfolio analysis\n"
        "🧹 /clear — reset conversation\n\n"
        "💬 *Just type anything* — ask about your portfolio, news, buy/sell ideas.\n\n"
        "_Auto briefing every day at 7:00am UK time_ ⏰",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_prices(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching live prices...")
    try:
        cp, sp = await fetch_all_prices(force=True)
        await send_safe(msg, build_prices_message(cp, sp))
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def cmd_brief(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Generating briefing...")
    try:
        await _run_briefing(context, reply_msg=msg)
    except Exception as e:
        await msg.edit_text(f"❌ Briefing failed: {e}")

async def cmd_summary(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Analysing portfolio...")
    try:
        cp, sp = await fetch_all_prices()
        reply  = await ask_groq(
            "Give me an honest portfolio analysis. "
            "Top winners and losers by P&L%. "
            "Total value vs total invested. "
            "Which positions are worth holding and which look like dead weight? "
            "Be direct and sharp — not a list of bullet points about what each coin 'could' do.",
            cp, sp
        )
        await send_safe(msg, f"📋 *Portfolio Analysis*\n\n{reply}")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def cmd_clear(update, context: ContextTypes.DEFAULT_TYPE):
    global history
    history = []
    await update.message.reply_text("🧹 Conversation cleared. Fresh start!")

async def handle_message(update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    msg = await update.message.reply_text("🤔 Thinking...")
    try:
        cp, sp = await fetch_all_prices()
        reply  = await ask_groq(user_text, cp, sp)
        await send_safe(msg, reply)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

# ── Morning Briefing ───────────────────────────────────────────────────────────

async def _run_briefing(context: ContextTypes.DEFAULT_TYPE, reply_msg=None):
    now = datetime.now(UK_TZ).strftime("%A %d %B %Y")
    cp, sp = await fetch_all_prices()
    movers = get_top_movers(cp, sp)
    world  = "\n".join(fetch_rss(NEWS_FEEDS,   limit_per_feed=4)[:20]) or "No headlines"
    crypto = "\n".join(fetch_rss(CRYPTO_FEEDS, limit_per_feed=3)[:10]) or "No crypto news"

    prompt = f"""Write my morning briefing for {now}. Sharp, concise, no fluff.

WORLD/MACRO HEADLINES:
{world}

CRYPTO HEADLINES:
{crypto}

TOP PORTFOLIO MOVERS: {movers}

{portfolio_for_ai(cp, sp)}

Use EXACTLY this format:

🌍 *GEOPOLITICAL*
• Only truly significant US/EU/Russia/China events. Wars, sanctions, major shifts. Skip minor stuff.

📊 *MACRO ECONOMY*
• CPI, inflation, Fed/ECB moves, major data releases. Only include if relevant today.

🪙 *CRYPTO & PORTFOLIO NEWS*
• Key crypto market developments. Any news related to coins I hold or Bitdeer (BTDR).

📈 *TOP MOVERS*
• Biggest 24h moves in my portfolio with brief context on why.

💡 *KEY INSIGHT*
One sharp, actionable observation for today.

Max 350 words. Skip any section with nothing genuinely important."""

    reply = await ask_groq(prompt, cp, sp, use_big_model=True)
    text  = f"🌅 *Morning Briefing — {now}*\n\n{reply}"
    if reply_msg:
        await send_safe(reply_msg, text)
    else:
        await send_safe(context, text, chat_id=CHAT_ID)

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled morning briefing...")
    try:
        await _run_briefing(context)
    except Exception as e:
        logger.error(f"Morning briefing failed: {e}")
        await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Morning briefing error: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("prices",  cmd_prices))
    app.add_handler(CommandHandler("brief",   cmd_brief))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("clear",   cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(
        morning_briefing,
        time=dtime(hour=7, minute=0, tzinfo=UK_TZ),
        name="morning_briefing"
    )

    logger.info("🚀 Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

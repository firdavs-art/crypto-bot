import os
import asyncio
import logging
from datetime import datetime
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config from environment variables ──────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
CHAT_ID     = os.environ["CHAT_ID"]
# Optional: set a % threshold for price alerts, default 10%
ALERT_PCT   = float(os.getenv("ALERT_PCT", "10"))

# ── Your portfolio (coin_id as used by CoinGecko : quantity) ───────────────────
PORTFOLIO = {
    "cosmos":           225.00,
    "layerzero":        142.50,
    "cow-protocol":    1000.00,
    "pudgy-penguins":25000.00,
    "dogecoin":        1750.00,
    "zksync":          5000.00,
    "researchcoin":    1000.00,
    "solana":             1.00,
    "fuel-network":   60014.00,
    "across-protocol": 2000.00,
    "aptos":             50.00,
    "wormhole":        2500.00,
    "arbius":           100.00,
    "solforge":        1000.00,
    "persistence":     2621.00,
    "data-lake":     270000.00,
}

# Cost basis per coin (USD) — your original invested amounts
COST_BASIS = {
    "cosmos":          1258.24,
    "layerzero":        185.00,
    "cow-protocol":     333.90,
    "pudgy-penguins":   537.87,
    "dogecoin":         159.90,
    "zksync":           289.19,
    "researchcoin":     300.00,
    "solana":           175.00,
    "fuel-network":     402.99,
    "across-protocol":  288.85,
    "aptos":            193.00,
    "wormhole":          51.40,
    "arbius":          2663.00,
    "solforge":         385.18,
    "persistence":       28.12,
    "data-lake":        232.26,
}

COIN_SYMBOLS = {
    "cosmos": "ATOM", "layerzero": "ZRO", "cow-protocol": "COW",
    "pudgy-penguins": "PENGU", "dogecoin": "DOGE", "zksync": "ZK",
    "researchcoin": "RSC", "solana": "SOL", "fuel-network": "FUEL",
    "across-protocol": "ACX", "aptos": "APT", "wormhole": "W",
    "arbius": "AIUS", "solforge": "SFG", "persistence": "XPRT",
    "data-lake": "LAKE",
}

# ── API helpers ────────────────────────────────────────────────────────────────

async def fetch_prices() -> dict:
    """Fetch live prices from CoinGecko (free, no key needed)."""
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
    """Fetch Fear & Greed index from alternative.me."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://api.alternative.me/fng/?limit=1")
        r.raise_for_status()
        data = r.json()["data"][0]
        return {"value": data["value"], "label": data["value_classification"]}

async def fetch_news() -> list[dict]:
    """Fetch top crypto headlines from CryptoPanic public feed (no key needed)."""
    url = "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&public=true&kind=news&filter=hot"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            results = r.json().get("results", [])
            return [
                {"title": item["title"], "url": item["url"]}
                for item in results[:5]
            ]
    except Exception:
        return []

# ── Message builders ───────────────────────────────────────────────────────────

def build_portfolio_section(prices: dict) -> tuple[str, float, float]:
    """Returns formatted portfolio text, total value, total P&L."""
    lines = []
    total_value = 0.0
    total_cost   = sum(COST_BASIS.values())

    gainers, losers = [], []

    for coin_id, qty in PORTFOLIO.items():
        symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
        data   = prices.get(coin_id, {})
        price  = data.get("usd", 0)
        chg24  = data.get("usd_24h_change", 0) or 0
        value  = price * qty
        cost   = COST_BASIS.get(coin_id, 0)
        pnl    = value - cost
        pnl_pct= (pnl / cost * 100) if cost else 0

        total_value += value

        arrow = "🟢" if chg24 >= 0 else "🔴"
        chg_str = f"{chg24:+.2f}%"
        pnl_str = f"${pnl:+.2f} ({pnl_pct:+.1f}%)"

        lines.append(
            f"{arrow} *{symbol}* — ${value:.2f}  _{chg_str} 24h_\n"
            f"   P&L: {pnl_str}"
        )

        if chg24 >= ALERT_PCT:
            gainers.append(f"🚀 *{symbol}* surged {chg24:+.1f}% in 24h!")
        elif chg24 <= -ALERT_PCT:
            losers.append(f"⚠️ *{symbol}* dropped {chg24:.1f}% in 24h!")

    total_pnl     = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0

    section  = "\n".join(lines)
    section += (
        f"\n\n💼 *Portfolio Total: ${total_value:,.2f}*\n"
        f"📊 Overall P&L: ${total_pnl:+,.2f} ({total_pnl_pct:+.1f}%)"
    )

    alert_section = ""
    if gainers or losers:
        alert_section = "\n\n🔔 *Price Alerts*\n" + "\n".join(gainers + losers)

    return section + alert_section, total_value, total_pnl


def build_daily_message(prices: dict, fg: dict, news: list[dict]) -> str:
    date_str  = datetime.utcnow().strftime("%a %d %b %Y, %H:%M UTC")
    fg_emoji  = {"Extreme Fear": "😱", "Fear": "😰", "Neutral": "😐",
                 "Greed": "😏", "Extreme Greed": "🤑"}.get(fg["label"], "📊")

    portfolio_text, total_val, total_pnl = build_portfolio_section(prices)

    news_lines = ""
    if news:
        news_lines = "\n\n📰 *Top Crypto News*\n" + "\n".join(
            f"• [{item['title']}]({item['url']})" for item in news
        )
    else:
        news_lines = "\n\n📰 _No news fetched — check CryptoPanic manually._"

    return (
        f"🌅 *Daily Crypto Update*\n_{date_str}_\n\n"
        f"😨 *Market Sentiment*: {fg_emoji} {fg['label']} ({fg['value']}/100)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Your Portfolio*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{portfolio_text}"
        f"{news_lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Next update tomorrow_ ✌️"
    )

# ── Bot commands ───────────────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Crypto Portfolio Bot is live!*\n\n"
        "Commands:\n"
        "/update — get an update right now\n"
        "/prices — live prices only\n"
        "/help — show this message",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_update(update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching data...")
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
            data   = prices.get(coin_id, {})
            price  = data.get("usd", 0)
            chg24  = data.get("usd_24h_change", 0) or 0
            arrow  = "🟢" if chg24 >= 0 else "🔴"
            lines.append(f"{arrow} *{symbol}*: ${price} ({chg24:+.2f}%)")
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
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
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        logger.info("Daily update sent.")
    except Exception as e:
        logger.error(f"Failed to send daily update: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("prices", cmd_prices))

    # Schedule daily update at 08:00 UTC
    job_queue = app.job_queue
    job_queue.run_daily(
        send_daily_update,
        time=datetime.strptime("08:00", "%H:%M").time(),
        name="daily_update"
    )

    logger.info("Bot started. Listening for commands...")
    app.run_polling()

if __name__ == "__main__":
    main()

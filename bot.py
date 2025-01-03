#!/usr/bin/env python3
import os
import re
import logging
import requests
from datetime import datetime
from urllib.parse import quote

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ==========================================
# 1. Load Environment Variables
# ==========================================
load_dotenv()  # Load from .env

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

if not TELEGRAM_BOT_TOKEN or not MONGODB_URI:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or MONGODB_URI in .env")

# ==========================================
# 2. Logging Configuration
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 3. MongoDB Setup
# ==========================================
client = MongoClient(MONGODB_URI)
db = client["snipe_checks"]  # Database name
picks_collection = db["picks"]

# Optionally ensure indexes for better performance (not strictly required):
# picks_collection.create_index("user_id")
# picks_collection.create_index("mint_address")

# ==========================================
# 4. Pump.fun API Functions
# ==========================================
def get_sol_price() -> float:
    """
    Fetch the current SOL price in USD from Pump.fun.
    Endpoint: https://frontend-api-v2.pump.fun/sol-price
    Returns 0.0 on error.
    """
    url = "https://frontend-api-v2.pump.fun/sol-price"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()  # e.g. {"solPrice": 213}
        return data.get("solPrice", 0.0)
    except Exception as e:
        logger.error(f"Error fetching SOL price: {e}")
        return 0.0


def get_latest_close_price_in_sol(mint_address: str) -> float:
    """
    Fetch the latest 1-min candlestick from Pump.fun for the given mint_address.
    Returns the 'close' price in SOL or 0.0 on error.
    Example endpoint:
      https://frontend-api-v2.pump.fun/candlesticks/<MINT>?offset=0&limit=1&timeframe=1
    """
    base_url = f"https://frontend-api-v2.pump.fun/candlesticks/{mint_address}"
    params = {
        "offset": "0",
        "limit": "1",
        "timeframe": "1"  # 1-min candles
    }
    try:
        resp = requests.get(base_url, params=params, timeout=10)
        resp.raise_for_status()
        csticks = resp.json()
        if not csticks:
            return 0.0
        latest_candle = csticks[-1]
        return float(latest_candle.get("close", 0.0))
    except Exception as e:
        logger.error(f"Error fetching candlestick for {mint_address}: {e}")
        return 0.0

# ==========================================
# 5. Utility: Validate Solana Address
# ==========================================
def is_valid_solana_address(address: str) -> bool:
    """
    Simple check:
    - Typically 43 or 44 chars in length,
    - Base58 with certain excluded characters.
    This is not perfect, but it filters out obvious invalid strings.
    """
    if len(address) not in [43, 44]:
        return False
    pattern = r'^[1-9A-HJ-NP-Za-km-z]+$'
    return bool(re.match(pattern, address))

# ==========================================
# 6. Bot Handlers
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command.
    """
    welcome_text = (
    "Welcome to the Snipe Checks Bot 🤖✨\n\n"
    "With our bot you can do 2 very cool things:\n"
    "1) 🏹 Enter/Shill any CA (Solana contract address) in the chat. The bot tracks "
    "how much profit or loss that CA makes in real time and who shilled it.\n"
    "   This helps call groups see who shills profitable coins—and who doesn't! 🚀\n\n"
    "2) 🏆 Check the leaderboard to see the best picks. 🔥\n\n"
    "Commands:\n"
    "  /leaderboard - View the PnL leaderboard 📈\n"
    "  /share - Share your current picks on Twitter 🐦\n"
    "\nJust paste any valid Solana CA in the chat to add your pick! 🌀"
)

    await update.message.reply_text(welcome_text)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /leaderboard: Calculate PnL for each pick across all users, then rank.
    """
    # Fetch all picks from MongoDB
    all_picks = list(picks_collection.find({}))
    if not all_picks:
        await update.message.reply_text("No picks found yet. Paste a CA to add your first pick!")
        return

    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("Error: Could not fetch SOL price. Leaderboard unavailable.")
        return

    leaderboard_data = []
    for pick in all_picks:
        user_id = pick["user_id"]
        username = pick["username"]
        mint = pick["mint_address"]
        entry_price_usd = pick["entry_price_usd"]
        num_tokens = pick["num_tokens"]

        current_close_sol = get_latest_close_price_in_sol(mint)
        current_price_usd = current_close_sol * sol_price

        pnl = num_tokens * (current_price_usd - entry_price_usd)

        leaderboard_data.append({
            "user_id": user_id,
            "username": username,
            "mint": mint,
            "entry_price_usd": entry_price_usd,
            "current_price_usd": current_price_usd,
            "pnl": pnl
        })

    # Sort descending by PnL
    leaderboard_data.sort(key=lambda x: x["pnl"], reverse=True)

    result_text = "🏆 *Snipe Checks Leaderboard:* 🏆\n\n"
    top_n = leaderboard_data[:10]
    for rank, item in enumerate(top_n, start=1):
        sign = "+" if item["pnl"] >= 0 else "-"
        abs_pnl = abs(item["pnl"])
        result_text += (
            f"{rank}. {item['username']} (Mint: `{item['mint']}`)\n"
            f"   PnL: {sign}${abs_pnl:,.2f}\n"
            f"   Entry: ${item['entry_price_usd']:.8f}\n"
            f"   Current: ${item['current_price_usd']:.8f}\n\n"
        )

    await update.message.reply_text(result_text, parse_mode="Markdown")


async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /share: Let user share their picks on Twitter.
    - Summarizes each pick's PnL, then total PnL, then link to Twitter.
    """
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anonymous"

    user_picks = list(picks_collection.find({"user_id": user_id}))
    if not user_picks:
        await update.message.reply_text("You have no picks yet. Paste a CA to add your first pick!")
        return

    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("Error: Could not fetch SOL price. Cannot share now.")
        return

    lines = []
    total_pnl = 0.0

    for pick in user_picks:
        mint = pick["mint_address"]
        entry_price_usd = pick["entry_price_usd"]
        num_tokens = pick["num_tokens"]

        current_close_sol = get_latest_close_price_in_sol(mint)
        current_price_usd = current_close_sol * sol_price
        pnl = num_tokens * (current_price_usd - entry_price_usd)
        total_pnl += pnl

        sign = "+" if pnl >= 0 else "-"
        abs_pnl = abs(pnl)
        lines.append(f"{mint} => {sign}${abs_pnl:,.2f}")

    sign_total = "+" if total_pnl >= 0 else "-"
    abs_total = abs(total_pnl)

    tweet_text = (
        f"{username}'s Snipe Checks Picks:\n\n"
        + "\n".join(lines)
        + f"\n\nTotal PnL: {sign_total}${abs_total:,.2f}\n"
        "Shared via #SnipeChecksBot"
    )
    encoded_tweet = quote(tweet_text)
    twitter_link = f"https://twitter.com/intent/tweet?text={encoded_tweet}"

    msg = (
        f"Share your picks on Twitter:\n\n"
        f"[Click Here to Tweet]({twitter_link})"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def handle_contract_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle text that might be a Solana CA. If valid, 'invest' $100 and store in MongoDB.
    """
    text = update.message.text.strip()
    if not is_valid_solana_address(text):
        return  # Not a valid address => fallback

    mint_address = text
    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("Error: Could not fetch SOL price. Try again later.")
        return

    close_price_sol = get_latest_close_price_in_sol(mint_address)
    if close_price_sol <= 0:
        await update.message.reply_text(
            f"Error: Could not fetch a valid close price for CA: {mint_address}"
        )
        return

    entry_price_usd = close_price_sol * sol_price
    if entry_price_usd <= 0:
        await update.message.reply_text(
            "Error: The price is 0 or invalid. Possibly illiquid or no data on Pump.fun."
        )
        return

    num_tokens = 100.0 / entry_price_usd  # "invest" $100
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anonymous"

    # Store in MongoDB
    pick_doc = {
        "user_id": user_id,
        "username": username,
        "mint_address": mint_address,
        "entry_price_usd": entry_price_usd,
        "num_tokens": num_tokens,
        "created_at": datetime.utcnow()
    }
    picks_collection.insert_one(pick_doc)

    reply_text = (
        f"✅ Added your pick for CA: {mint_address}\n"
        f"Entry Price (USD): ${entry_price_usd:.8f}\n"
        f"You 'bought' {num_tokens:.6f} tokens with $100.\n\n"
        f"Use /leaderboard to see how your pick ranks!\n"
        f"Use /share to share your picks on Twitter."
    )
    await update.message.reply_text(reply_text)


async def fallback_echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    For other text that isn't a valid CA or command, we can echo or ignore.
    """
    await update.message.reply_text(f"You said: {update.message.text}")


# ==========================================
# 7. Main Bot Setup & Run
# ==========================================
def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("share", share_command))

    # Text handler for potential CA
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_contract_address))

    # Fallback echo
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_echo))

    logger.info("Starting Snipe Checks Bot with MongoDB persistence...")
    application.run_polling()


if __name__ == "__main__":
    main()

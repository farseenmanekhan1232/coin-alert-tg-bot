import os
import re
import logging
import uuid
import httpx  # For asynchronous HTTP requests
from datetime import datetime
from urllib.parse import quote
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
import telegram  # For logging the library version
from cachetools import TTLCache, cached

# ==========================
# 1. Load Environment Variables
# ==========================

load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')
COINMARKETCAP_API_KEY = os.getenv('COINMARKETCAP_API_KEY')

if not TELEGRAM_BOT_TOKEN or not MONGODB_URI or not COINMARKETCAP_API_KEY:
    raise ValueError("One or more environment variables are missing. Please check your .env file.")

# ==========================
# 2. Configure Logging
# ==========================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Log the python-telegram-bot version
logger.info(f"python-telegram-bot version: {telegram.__version__}")

# ==========================
# 3. Initialize MongoDB
# ==========================

client = MongoClient(MONGODB_URI)
db = client['crypto_tracker']
users_collection = db['users']
calls_collection = db['calls']

# Ensure unique index on telegram_id
users_collection.create_index('telegram_id', unique=True)

# ==========================
# 4. Initialize Caching
# ==========================

# Cache for current prices with a TTL of 60 seconds
price_cache = TTLCache(maxsize=100, ttl=60)

# ==========================
# 5. Helper Functions
# ==========================

def validate_sol_address(address: str) -> bool:
    """
    Validates Solana wallet and contract addresses.
    Solana addresses are base58 encoded strings, typically 43-44 characters.
    """
    if len(address) not in [43, 44]:
        return False
    base58_pattern = '^[1-9A-HJ-NP-Za-km-z]+$'
    return re.match(base58_pattern, address) is not None

@cached(cache=price_cache)
async def get_current_prices(crypto_symbols: list) -> dict:
    """
    Fetches the current prices for a list of cryptocurrencies in USD using CoinMarketCap API.
    Returns a dictionary mapping symbol to its current price.
    """
    url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
    headers = {
        'Accepts': 'application/json',
        'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY,
    }
    symbols_str = ','.join([symbol.upper() for symbol in crypto_symbols])
    params = {
        'symbol': symbols_str,
        'convert': 'USD'
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            prices = {}
            if 'data' in data:
                for symbol in crypto_symbols:
                    upper_symbol = symbol.upper()
                    if upper_symbol in data['data']:
                        prices[upper_symbol] = data['data'][upper_symbol]['quote']['USD']['price']
            logger.info(f"Fetched current prices for symbols: {prices}")
            return prices
        except httpx.RequestError as e:
            logger.error(f"An error occurred while requesting CoinMarketCap API: {e}")
            return {}
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred: {e}")
            return {}

def calculate_profit(call, current_prices: dict) -> float:
    """
    Calculates profit or loss based on the current price and the price at the time of the call.
    """
    symbol = call['crypto_symbol']
    if symbol not in current_prices:
        return 0.0  # Or handle as needed
    current_price = current_prices[symbol]
    action = call['action']
    price_at_call = call['price_at_call']
    number_of_units = call['number_of_units']
    if action == 'BUY':
        pnl = (current_price - price_at_call) * number_of_units
    elif action == 'SELL':
        pnl = (price_at_call - current_price) * number_of_units
    else:
        pnl = 0.0
    logger.info(f"Calculated PnL for user {call['user_id']}: {pnl}")
    return pnl

def get_or_create_user(telegram_id, username):
    """
    Retrieves the user from the database or creates a new record if not found.
    """
    try:
        user = users_collection.find_one({"telegram_id": telegram_id})
        if user:
            return user
        else:
            user_data = {
                "telegram_id": telegram_id,
                "username": username if username else "",
                "wallets": [],
                "contract_addresses": [],
                "created_at": datetime.utcnow()
            }
            users_collection.insert_one(user_data)
            logger.info(f"Created new user with Telegram ID {telegram_id}")
            return users_collection.find_one({"telegram_id": telegram_id})
    except DuplicateKeyError:
        logger.warning(f"Duplicate entry for Telegram ID {telegram_id}")
        return users_collection.find_one({"telegram_id": telegram_id})
    except Exception as e:
        logger.error(f"Error retrieving or creating user {telegram_id}: {e}")
        return None

def log_call(user_id, crypto_symbol, action, number_of_units, wallet_id):
    """
    Logs a BUY or SELL transaction.
    """
    price_at_call = get_current_price_at_call(crypto_symbol)  # Implement actual price fetching logic
    call_data = {
        "user_id": user_id,
        "crypto_symbol": crypto_symbol,
        "action": action,
        "number_of_units": number_of_units,
        "wallet_id": wallet_id,
        "price_at_call": price_at_call,
        "timestamp": datetime.utcnow()
    }
    calls_collection.insert_one(call_data)
    logger.info(f"Logged {action} call for user {user_id}: {call_data}")

def get_sol_balance(address: str) -> float:
    """
    Placeholder function to get SOL balance.
    Replace with actual implementation.
    """
    return 0.0

def get_current_price_at_call(crypto_symbol: str) -> float:
    """
    Placeholder function to get the price at the time of transaction logging.
    Replace with actual implementation.
    """
    return 0.0

# ==========================
# 6. Define States for Conversation Handlers
# ==========================

# Add Wallet States
ADD_WALLET = 1

# Buy/Sell Transaction States
TRANSACTION_GET_ACTION, TRANSACTION_GET_SYMBOL, TRANSACTION_GET_UNITS, TRANSACTION_SELECT_WALLET = range(4)

# ==========================
# 7. Command Handlers
# ==========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /start command. Provides an interactive welcome message with options.
    """
    try:
        welcome_text = (
            "👋 Welcome to the Crypto Tracker Bot!\n\n"
            "Manage your cryptocurrency portfolio effortlessly. Choose an option below to get started:"
        )
        keyboard = [
            [InlineKeyboardButton("📥 Add Wallet", callback_data='add_wallet')],
            [InlineKeyboardButton("💰 Buy/Sell Crypto", callback_data='buy_sell')],
            [InlineKeyboardButton("📊 View Portfolio", callback_data='portfolio')],
            [InlineKeyboardButton("📈 Leaderboard", callback_data='leaderboard')],
            [InlineKeyboardButton("❓ Help", callback_data='help')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in /start command: {e}")
        await update.effective_message.reply_text('❌ An error occurred while processing your request.')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /help command. Provides an interactive help menu.
    """
    try:
        help_text = "📚 *Help Menu:*\n\nChoose a topic below to learn more."
        keyboard = [
            [InlineKeyboardButton("🔍 Manage Wallets", callback_data='help_wallets')],
            [InlineKeyboardButton("💰 Transactions", callback_data='help_transactions')],
            [InlineKeyboardButton("📊 Portfolio", callback_data='help_portfolio')],
            [InlineKeyboardButton("🏆 Leaderboard", callback_data='help_leaderboard')],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in /help command: {e}")
        await update.effective_message.reply_text('❌ An error occurred while processing your request.')

async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /share command. Shares the latest transaction on Twitter.
    """
    try:
        telegram_id = update.effective_user.id
        user = users_collection.find_one({"telegram_id": telegram_id})
        if not user:
            await update.effective_message.reply_text("❌ User not found. Please start with /start.")
            return
        latest_call = calls_collection.find_one(
            {"user_id": user['_id']},
            sort=[("timestamp", -1)]
        )
        if not latest_call:
            await update.effective_message.reply_text("❌ No transactions to share.")
            return
        # Retrieve current price for the latest crypto
        current_prices = await get_current_prices([latest_call['crypto_symbol']])
        # Calculate profit
        profit = calculate_profit(latest_call, current_prices)
        if profit is None:
            await update.effective_message.reply_text("❌ Unable to calculate profit at this time.")
            return
        # Prepare tweet content
        action = latest_call['action']
        crypto = latest_call['crypto_symbol']
        price = latest_call['price_at_call']
        number_of_units = latest_call['number_of_units']
        investment_amount = latest_call.get('investment_amount', price * number_of_units)
        current_price = current_prices.get(crypto, price)  # Use price at call if current price unavailable
        current_value = current_price * number_of_units if current_price else investment_amount
        profit_text = f"+${profit:,.2f}" if profit >= 0 else f"-${abs(profit):,.2f}"
        username = user['username'] if user['username'] else "CryptoUser"
        tweet = (
            f"{username} executed a {action} on {crypto}.\n"
            f"Number of Units: {number_of_units}\n"
            f"Price at Call: ${price:,.2f}\n"
            f"Investment Amount: ${investment_amount:,.2f}\n"
            f"Current Value: ${current_value:,.2f}\n"
            f"Profit/Loss: {profit_text}"
        )
        # Encode tweet
        encoded_tweet = quote(tweet)
        twitter_share_link = f"https://twitter.com/intent/tweet?text={encoded_tweet}"
        await update.effective_message.reply_text(
            f"🔗 [Share to Twitter]({twitter_share_link})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error in /share command: {e}")
        await update.effective_message.reply_text('❌ An error occurred while generating the share link.')

# ==========================
# 8. Conversation Handlers
# ==========================

# --------------------------
# Add Wallet Conversation
# --------------------------
async def addwallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Initiates the Add Wallet conversation.
    """
    await update.effective_message.reply_text('📥 Please enter your Solana (SOL) wallet address or type /cancel to abort:')
    return ADD_WALLET

async def addwallet_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the wallet address and processes it.
    """
    try:
        wallet_address = update.effective_message.text.strip()
        if not validate_sol_address(wallet_address):
            await update.effective_message.reply_text('❌ Invalid SOL wallet address. Please try again or type /cancel to abort:')
            return ADD_WALLET
        telegram_id = update.effective_user.id
        username = update.effective_user.username
        user = get_or_create_user(telegram_id, username)
        if user is None:
            await update.effective_message.reply_text('❌ Error creating or retrieving your user profile. Please try again later.')
            return ConversationHandler.END
        # Generate a unique wallet_id
        wallet_id = str(uuid.uuid4())[:8]  # Shortened for user-friendliness
        wallet_data = {
            "wallet_id": wallet_id,
            "sol_wallet_address": wallet_address,
            "sol_balance": 0.0,
            "created_at": datetime.utcnow()
        }
        users_collection.update_one(
            {"telegram_id": telegram_id},
            {"$push": {"wallets": wallet_data}}
        )
        await update.effective_message.reply_text(
            f'✅ Successfully added SOL wallet address:\n`{wallet_address}`\n*Wallet ID:* `{wallet_id}`',
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in addwallet_receive: {e}")
        await update.effective_message.reply_text('❌ An error occurred while adding your wallet. Please try again later.')
        return ConversationHandler.END

async def addwallet_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Cancels the Add Wallet conversation.
    """
    await update.effective_message.reply_text('🛑 Wallet addition canceled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --------------------------
# Buy/Sell Transaction Conversation
# --------------------------
async def buy_sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Initiates the Buy/Sell transaction conversation.
    """
    keyboard = [
        [InlineKeyboardButton("💰 Buy", callback_data='BUY')],
        [InlineKeyboardButton("💸 Sell", callback_data='SELL')],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text('Please select the type of transaction:', reply_markup=reply_markup)
    return TRANSACTION_GET_ACTION

async def transaction_select_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the transaction type (BUY or SELL) and proceeds.
    """
    try:
        query = update.callback_query
        await query.answer()
        action = query.data

        if action in ['BUY', 'SELL']:
            context.user_data['transaction_action'] = action
            await query.edit_message_text(f'🔄 You selected *{action}*.\n\nPlease enter the cryptocurrency symbol (e.g., SOL):', parse_mode=ParseMode.MARKDOWN)
            return TRANSACTION_GET_SYMBOL
        elif action == 'back_to_main':
            await start_command(update, context)
            return ConversationHandler.END
        else:
            await query.edit_message_text("❓ Please select a valid transaction type.")
            return TRANSACTION_GET_ACTION
    except Exception as e:
        logger.error(f"Error in transaction_select_action: {e}")
        await update.effective_message.reply_text('❌ An error occurred. Please try again.')
        return ConversationHandler.END

async def transaction_get_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the cryptocurrency symbol.
    """
    try:
        crypto_symbol = update.effective_message.text.strip().upper()
        if not re.match(r'^[A-Z]{1,5}$', crypto_symbol):
            await update.effective_message.reply_text('❌ Invalid cryptocurrency symbol. Please enter a valid symbol (e.g., SOL):')
            return TRANSACTION_GET_SYMBOL
        context.user_data['crypto_symbol'] = crypto_symbol
        await update.effective_message.reply_text('🔢 Please enter the number of units:', reply_markup=ReplyKeyboardRemove())
        return TRANSACTION_GET_UNITS
    except Exception as e:
        logger.error(f"Error in transaction_get_symbol: {e}")
        await update.effective_message.reply_text('❌ An error occurred. Please try again.')
        return ConversationHandler.END

async def transaction_get_units(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the number of units and proceeds to wallet selection.
    """
    try:
        try:
            number_of_units = float(update.effective_message.text.strip())
            if number_of_units <= 0:
                raise ValueError
            context.user_data['number_of_units'] = number_of_units
        except ValueError:
            await update.effective_message.reply_text('❌ Invalid number of units. Please enter a positive numerical value:')
            return TRANSACTION_GET_UNITS

        telegram_id = update.effective_user.id
        user = users_collection.find_one({"telegram_id": telegram_id})
        if not user or not user.get('wallets'):
            await update.effective_message.reply_text('❌ No wallets found. Please add a wallet using /addwallet.')
            return ConversationHandler.END

        # Present wallet options
        keyboard = [
            [InlineKeyboardButton(f"{w['sol_wallet_address']}", callback_data=w['wallet_id'])] for w in user['wallets']
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text('🔄 Please select a wallet to use for this transaction:', reply_markup=reply_markup)
        return TRANSACTION_SELECT_WALLET
    except Exception as e:
        logger.error(f"Error in transaction_get_units: {e}")
        await update.effective_message.reply_text('❌ An error occurred. Please try again.')
        return ConversationHandler.END

async def transaction_select_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives the selected wallet and logs the transaction.
    """
    try:
        query = update.callback_query
        await query.answer()
        wallet_id = query.data
        context.user_data['selected_wallet_id'] = wallet_id

        # Retrieve user data
        telegram_id = update.effective_user.id
        user = users_collection.find_one({"telegram_id": telegram_id})
        wallet = next((w for w in user.get('wallets', []) if w['wallet_id'] == wallet_id), None)
        if not wallet:
            await query.edit_message_text(f'❌ Wallet `{wallet_id}` not found.')
            return ConversationHandler.END

        # Log the transaction
        user_id = user['_id']
        crypto_symbol = context.user_data['crypto_symbol']
        action = context.user_data['transaction_action']
        number_of_units = context.user_data['number_of_units']
        log_call(user_id, crypto_symbol, action, number_of_units, wallet_id)

        # Update wallet balance (placeholder)
        sol_balance = get_sol_balance(wallet['sol_wallet_address'])
        users_collection.update_one(
            {"telegram_id": telegram_id, "wallets.wallet_id": wallet_id},
            {"$set": {"wallets.$.sol_balance": sol_balance}}
        )

        # Fetch current price
        current_prices = await get_current_prices([crypto_symbol])
        current_price = current_prices.get(crypto_symbol, 0.0)

        # Prepare confirmation message
        if action == 'BUY':
            investment_amount = current_price * number_of_units
            confirmation_text = (
                f'✅ *BUY Transaction Logged:*\n\n'
                f"*Crypto:* `{crypto_symbol}`\n"
                f"*Units:* `{number_of_units}`\n"
                f"*Price per Unit:* `${current_price:,.2f}`\n"
                f"*Investment Amount:* `${investment_amount:,.2f}`\n"
                f"*Wallet ID:* `{wallet_id}`\n"
                f"*Current Balance:* `{sol_balance:,.2f} SOL`"
            )
        elif action == 'SELL':
            confirmation_text = (
                f'✅ *SELL Transaction Logged:*\n\n'
                f"*Crypto:* `{crypto_symbol}`\n"
                f"*Units:* `{number_of_units}`\n"
                f"*Price per Unit:* `${current_price:,.2f}`\n"
                f"*Wallet ID:* `{wallet_id}`\n"
                f"*Current Balance:* `{sol_balance:,.2f} SOL`"
            )
        else:
            confirmation_text = '✅ Transaction logged successfully.'

        await query.edit_message_text(confirmation_text, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in transaction_select_wallet: {e}")
        await query.edit_message_text('❌ An error occurred while logging your transaction. Please try again.')
        return ConversationHandler.END

async def buy_sell_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Cancels the Buy/Sell transaction conversation.
    """
    await update.effective_message.reply_text('🛑 Transaction canceled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ==========================
# 9. Portfolio Command
# ==========================
async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /portfolio command. Provides a summary of all transactions and current balances.
    """
    try:
        telegram_id = update.effective_user.id
        user = users_collection.find_one({"telegram_id": telegram_id})
        if not user:
            await update.effective_message.reply_text("❌ User not found. Please start with /start.")
            return

        calls = list(calls_collection.find({"user_id": user['_id']}))
        if not calls and not user.get('wallets') and not user.get('contract_addresses'):
            await update.effective_message.reply_text("❌ No transactions, wallets, or Contract Addresses found.")
            return

        summary = "📊 *Your Portfolio:* \n\n"
        total_profit = 0.0

        # Retrieve all unique crypto symbols from user's transactions
        user_crypto_symbols = calls_collection.distinct("crypto_symbol", {"user_id": user['_id']})
        if user_crypto_symbols:
            current_prices = await get_current_prices(user_crypto_symbols)
        else:
            current_prices = {}

        # List all transactions
        if calls:
            summary += "*Transactions:*\n"
            for call in calls:
                pnl = calculate_profit(call, current_prices)
                pnl_text = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
                summary += (
                    f"• *{call['action']}* `{call['crypto_symbol']}`\n"
                    f"  *Units:* `{call['number_of_units']}`\n"
                    f"  *Price at Call:* `${call['price_at_call']:,.2f}`\n"
                    f"  *Profit/Loss:* `{pnl_text}`\n"
                    f"  *Wallet ID:* `{call['wallet_id']}`\n\n"
                )
                total_profit += pnl

        # List all wallets with their balances
        wallets = user.get('wallets', [])
        if wallets:
            summary += "*Wallets:* \n"
            for wallet in wallets:
                summary += (
                    f"• *ID:* `{wallet['wallet_id']}`\n"
                    f"  *Address:* `{wallet['sol_wallet_address']}`\n"
                    f"  *Balance:* `{wallet['sol_balance']:,.2f} SOL`\n\n"
                )

        # List all contract addresses with their balances
        contracts = user.get('contract_addresses', [])
        if contracts:
            summary += "*Contract Addresses:* \n"
            for contract in contracts:
                summary += (
                    f"• *ID:* `{contract['contract_id']}`\n"
                    f"  *Address:* `{contract['contract_address']}`\n"
                    f"  *Balance:* `{contract['contract_balance']:,.2f} SOL`\n\n"
                )

        # Total Profit/Loss
        summary += f"*Total Profit/Loss:* `{total_profit:,.2f}$`"
        await update.effective_message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in /portfolio command: {e}")
        await update.effective_message.reply_text('❌ An error occurred while generating your portfolio summary.')

# ==========================
# 10. Leaderboard Command
# ==========================
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the /leaderboard command.
    Displays a leaderboard of users ranked by their total PnL.
    """
    try:
        # Step 1: Retrieve all unique crypto symbols from transactions
        crypto_symbols = calls_collection.distinct("crypto_symbol")
        if not crypto_symbols:
            await update.effective_message.reply_text("❌ No transactions found to generate a leaderboard.")
            return

        # Step 2: Fetch current prices for all symbols
        current_prices = await get_current_prices(crypto_symbols)
        if not current_prices:
            await update.effective_message.reply_text("❌ Failed to fetch current cryptocurrency prices. Please try again later.")
            return

        # Step 3: Aggregate PnL for each user
        user_pnls = []
        users_cursor = users_collection.find({})
        for user in users_cursor:
            user_id = user['_id']
            username = user['username'] if user.get('username') else f"User {user['telegram_id']}"
            user_calls = calls_collection.find({"user_id": user_id})
            total_pnl = 0.0
            for call in user_calls:
                pnl = calculate_profit(call, current_prices)
                total_pnl += pnl
            user_pnls.append({"username": username, "pnl": total_pnl})

        # Step 4: Sort users by PnL descending
        user_pnls_sorted = sorted(user_pnls, key=lambda x: x['pnl'], reverse=True)

        if not user_pnls_sorted:
            await update.effective_message.reply_text("❌ No users with transactions found to generate a leaderboard.")
            return

        # Step 5: Prepare leaderboard message
        leaderboard_text = "🏆 *PnL Leaderboard:*\n\n"
        for rank, user in enumerate(user_pnls_sorted[:10], start=1):
            username = user['username']
            pnl = user['pnl']
            pnl_text = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            leaderboard_text += f"{rank}. *{username}*: {pnl_text}\n"

        await update.effective_message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in /leaderboard command: {e}")
        await update.effective_message.reply_text('❌ An error occurred while generating the leaderboard.')

# ==========================
# 11. Callback Query Handlers
# ==========================

async def help_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the help menu selections.
    """
    try:
        query = update.callback_query
        await query.answer()
        choice = query.data

        if choice == 'help_wallets':
            help_wallets = (
                "*Manage Wallets:*\n\n"
                "• `/addwallet <SOL_wallet_address>` - Add a new SOL wallet address.\n"
                "• `/listwallets` - View all your added wallets.\n"
                "• `/removewallet <wallet_id>` - Delete a specific wallet.\n"
                "• `/setdefaultwallet <wallet_id>` - Choose a default wallet for transactions.\n"
            )
            await query.edit_message_text(help_wallets, parse_mode=ParseMode.MARKDOWN)
        elif choice == 'help_transactions':
            help_transactions = (
                "*Transactions:*\n\n"
                "• `/buy` - Log a BUY transaction.\n"
                "• `/sell` - Log a SELL transaction.\n"
                "• `/balance` - Check balances of wallets or contracts.\n"
                "• `/portfolio` - View your portfolio summary.\n"
                "• `/share` - Share your latest transaction on Twitter.\n"
            )
            await query.edit_message_text(help_transactions, parse_mode=ParseMode.MARKDOWN)
        elif choice == 'help_portfolio':
            help_portfolio = (
                "*Portfolio:*\n\n"
                "View all your transactions, wallet balances, contract balances, and total Profit/Loss.\n"
            )
            await query.edit_message_text(help_portfolio, parse_mode=ParseMode.MARKDOWN)
        elif choice == 'help_leaderboard':
            help_leaderboard = (
                "*Leaderboard:*\n\n"
                "View a ranking of users based on their total Profit/Loss.\n"
            )
            await query.edit_message_text(help_leaderboard, parse_mode=ParseMode.MARKDOWN)
        elif choice == 'back_to_main':
            await start_command(update, context)
        else:
            await query.edit_message_text("❓ Please select a valid help topic.")
    except Exception as e:
        logger.error(f"Error in help_menu_handler: {e}")
        await query.edit_message_text('❌ An error occurred while processing your request.')

async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the main menu selections from the /start command.
    """
    try:
        query = update.callback_query
        await query.answer()
        choice = query.data

        if choice == 'add_wallet':
            await addwallet_start(update, context)
        elif choice == 'buy_sell':
            await buy_sell_start(update, context)
        elif choice == 'portfolio':
            await portfolio_command(update, context)
        elif choice == 'leaderboard':
            await leaderboard_command(update, context)
        elif choice == 'help':
            await help_command(update, context)
        else:
            await query.edit_message_text("❓ Please select a valid option.")
    except Exception as e:
        logger.error(f"Error in start_menu_handler: {e}")
        await update.effective_message.reply_text('❌ An error occurred. Please try again.')

# ==========================
# 12. Message Handler for Contract Addresses
# ==========================

async def handle_contract_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles messages that are Contract Addresses sent directly in the chat.
    """
    try:
        message_text = update.effective_message.text.strip()
        if not validate_sol_address(message_text):
            return  # Ignore non-CA messages

        telegram_id = update.effective_user.id
        username = update.effective_user.username
        user = get_or_create_user(telegram_id, username)
        if user is None:
            await update.effective_message.reply_text('❌ Error retrieving your user profile. Please try again later.')
            return

        # Check if the CA already exists for the user
        existing_contract = next((c for c in user.get('contract_addresses', []) if c['contract_address'] == message_text), None)
        if existing_contract:
            await update.effective_message.reply_text('⚠️ This Contract Address is already added to your profile.')
            return

        # Generate a unique contract_id
        contract_id = str(uuid.uuid4())[:8]  # Shortened for user-friendliness
        contract_data = {
            "contract_id": contract_id,
            "contract_address": message_text,
            "contract_balance": 0.0,
            "created_at": datetime.utcnow()
        }
        users_collection.update_one(
            {"telegram_id": telegram_id},
            {"$push": {"contract_addresses": contract_data}}
        )
        await update.effective_message.reply_text(
            f'✅ Successfully added Contract Address:\n`{message_text}`\n*Contract ID:* `{contract_id}`',
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in handle_contract_address: {e}")
        await update.effective_message.reply_text('❌ An error occurred while adding the Contract Address. Please try again later.')

# ==========================
# 13. Error Handler
# ==========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Catches all unhandled exceptions and logs them.
    """
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    # Notify the user about the error
    if isinstance(update, Update):
        if update.effective_message:
            try:
                await update.effective_message.reply_text('❌ An unexpected error occurred. Please try again later.')
            except Exception as e:
                logger.error(f"Failed to send error message to user: {e}")

# ==========================
# 14. Main Function
# ==========================

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("share", share_command))

    # Register Help Menu CallbackQueryHandler
    application.add_handler(CallbackQueryHandler(help_menu_handler, pattern='^(help_wallets|help_transactions|help_portfolio|help_leaderboard|back_to_main)$'))

    # Register Start Menu CallbackQueryHandler
    application.add_handler(CallbackQueryHandler(start_menu_handler, pattern='^(add_wallet|buy_sell|portfolio|leaderboard|help)$'))

    # Register Conversation Handlers
    add_wallet_conv = ConversationHandler(
        entry_points=[CommandHandler('addwallet', addwallet_start)],
        states={
            ADD_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, addwallet_receive)],
        },
        fallbacks=[CommandHandler('cancel', addwallet_cancel)],
    )
    application.add_handler(add_wallet_conv)

    buy_sell_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(transaction_select_action, pattern='^(BUY|SELL)$'), CommandHandler('buy', buy_sell_start), CommandHandler('sell', buy_sell_start)],
        states={
            TRANSACTION_GET_ACTION: [CallbackQueryHandler(transaction_select_action, pattern='^(BUY|SELL)$')],
            TRANSACTION_GET_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, transaction_get_symbol)],
            TRANSACTION_GET_UNITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, transaction_get_units)],
            TRANSACTION_SELECT_WALLET: [CallbackQueryHandler(transaction_select_wallet)],
        },
        fallbacks=[CommandHandler('cancel', buy_sell_cancel)],
    )
    application.add_handler(buy_sell_conv)

    # Register Leaderboard Command
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))

    # Register Portfolio Command
    application.add_handler(CommandHandler("portfolio", portfolio_command))

    # Register Share Command
    application.add_handler(CommandHandler("share", share_command))

    # Register Message Handler for Contract Addresses
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_contract_address))

    # Register the global error handler
    application.add_error_handler(error_handler)

    # Run the bot
    application.run_polling()

# ==========================
# 15. Entry Point
# ==========================

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🔴 Bot stopped by user.")

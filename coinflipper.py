#!/usr/bin/python3

import asyncpg
import logging
import random
from bitcoinrpc.authproxy import AuthServiceProxy
from bitcoinrpc.authproxy import JSONRPCException
from decimal import Decimal
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, CallbackContext

# Configure logging
LOG_FILE = "/var/log/coinflipper.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

RPC_USER = "rpcuser"
RPC_PASSWORD = "123"
RPC_HOST = "127.0.0.1"
RPC_PORT = 8332

DB_HOST = "127.0.0.1"
DB_NAME = "coinflipper"
DB_USER = "botuser"
DB_PASSWORD = "123"

coinflips = {}

async def coinflip(update: Update, context: CallbackContext):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /coinflip <sats> <number of participants>")
        return

    sats = int(context.args[0])
    max_participants = int(context.args[1])
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name
    chat_id = update.message.chat_id
    message = update.message

    # Log the coinflip initiation attempt
    logging.info(f"User {user_id} ({username}) initiated coinflip: entry={sats} sats, max_participants={max_participants} in chat {chat_id}.")

    if max_participants < 2:
        await update.message.reply_text("Minimum participants must be 2.")
        return

    # Check if user has enough balance
    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    await conn.close()

    if balance is None or balance < sats:
        logging.info(f"User {user_id} ({username}) has insufficient balance ({balance}) for coinflip entry of {sats} sats.")
        await update.message.reply_text("You don't have enough balance to start this coinflip.")
        return

    # Create coinflip entry with inline keyboard
    keyboard = [
        [InlineKeyboardButton("Join Coinflip", callback_data=f"join_{chat_id}_{message.message_id}")],
        [InlineKeyboardButton("Cancel Coinflip", callback_data=f"cancel_{chat_id}_{message.message_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        f"🎲 Coinflip started! {sats} sats entry. {max_participants} players needed.",
        reply_markup=reply_markup
    )

    coinflips[(chat_id, message.message_id)] = {
        "creator": user_id,
        "sats": sats,
        "max": max_participants,
        "participants": [],
        "start_time": datetime.utcnow()
    }

    logging.info(f"Coinflip created by user {user_id} ({username}) with message_id {msg.message_id} in chat {chat_id}.")

async def join_coinflip(update: Update, context: CallbackContext):
    logging.info(coinflips)
    query = update.callback_query
    _, chat_id, msg_id = query.data.split("_")
    chat_id, msg_id = int(chat_id), int(msg_id)
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.full_name

    # Log the join attempt
    logging.info(f"User {user_id} ({username}) attempting to join coinflip in chat {chat_id}, message {msg_id}.")

    if (chat_id, msg_id) not in coinflips:
        logging.warning(f"User {user_id} ({username}) attempted to join a non-existent coinflip in chat {chat_id}, message {msg_id}.")
        await query.answer("This coinflip no longer exists.")
        return

    coinflip = coinflips[(chat_id, msg_id)]

    # Auto-cancel if more than 2 hours passed
    if datetime.utcnow() - coinflip["start_time"] > timedelta(hours=2):
        logging.info(f"Coinflip in chat {chat_id}, message {msg_id} timed out. Canceling coinflip.")
        del coinflips[(chat_id, msg_id)]
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text="Coinflip canceled due to timeout."
        )
        return

    if user_id in [p[0] for p in coinflip["participants"]]:
        logging.info(f"User {user_id} ({username}) already joined coinflip in chat {chat_id}, message {msg_id}.")
        await query.answer("You have already joined.")
        return

    # Check balance
    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    await conn.close()
    if balance is None or balance < coinflip["sats"]:
        logging.info(f"User {user_id} ({username}) has insufficient balance ({balance}) to join coinflip requiring {coinflip['sats']} sats.")
        await query.answer("You don't have enough balance.")
        return

    # Add user to coinflip
    coinflip["participants"].append((user_id, username))
    logging.info(f"User {user_id} ({username}) successfully joined coinflip in chat {chat_id}, message {msg_id}. Total participants: {len(coinflip['participants'])}.")

    # Update the coinflip message with the new participant list
    participant_list = "\n".join([p[1] for p in coinflip["participants"]])
    keyboard = [
        [InlineKeyboardButton("Join Coinflip", callback_data=f"join_{chat_id}_{msg_id}")],
        [InlineKeyboardButton("Cancel Coinflip", callback_data=f"cancel_{chat_id}_{msg_id}")]
    ]
    await query.edit_message_text(
        text=f"🎲 Coinflip started! {coinflip['sats']} sats entry. {coinflip['max']} players needed.\n\nParticipants:\n{participant_list}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # If max participants reached, select winner
    if len(coinflip["participants"]) >= coinflip["max"]:
        logging.info(f"Coinflip in chat {chat_id}, message {msg_id} reached max participants. Determining winner...")
        for user in coinflip["participants"]:
            conn = await get_db_connection()
            balance = await get_user_balance(user[0], conn)
            await conn.close()
            sats = to_sats(balance)
            if sats < coinflip["sats"]:
                logging.warning(f"Some participants lack funds")
                await query.edit_message_text(
                    text=f"😳 Users lack balance to flip",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return  # Exit early if balances are insufficient

        logging.info(f"All participants have sufficient balance. Determining winner...")
        winner = random.choice(coinflip["participants"])
        winner_id, winner_name = winner
        total_prize = coinflip["sats"] * (coinflip["max"] - 1)

        conn = await get_db_connection()
        async with conn.transaction():
            for participant_id, _ in coinflip["participants"]:
                if participant_id != winner_id:
                    await conn.execute("UPDATE balances SET balance = balance - $1 WHERE user_id = $2", coinflip["sats"], participant_id)
            await conn.execute("UPDATE balances SET balance = balance + $1 WHERE user_id = $2", total_prize, winner_id)
        await conn.close()

        logging.info(f"Coinflip in chat {chat_id}, message {msg_id}: Winner is user {winner_id} ({winner_name}) winning {total_prize} sats.")
        emoji = random.choice(['🔥', '🎉', '🥂', '💹', '🦈', '🗽'])
        await query.edit_message_text(
            text=f"{emoji} {winner_name} won the coinflip and received {total_prize} sats!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        del coinflips[(chat_id, msg_id)]

async def cancel_coinflip(update: Update, context: CallbackContext):
    query = update.callback_query
    _, chat_id, msg_id = query.data.split("_")
    chat_id, msg_id = int(chat_id), int(msg_id)
    user_id = query.from_user.id

    logging.info(f"User {user_id} requested cancellation of coinflip in chat {chat_id}, message {msg_id}.")

    if (chat_id, msg_id) not in coinflips:
        logging.warning(f"User {user_id} attempted to cancel a non-existent coinflip in chat {chat_id}, message {msg_id}.")
        await query.answer("This coinflip no longer exists.")
        return

    coinflip = coinflips[(chat_id, msg_id)]
    if user_id != coinflip["creator"]:
        logging.info(f"User {user_id} is not the creator and cannot cancel coinflip in chat {chat_id}, message {msg_id}.")
        await query.answer("Only the creator can cancel.")
        return

    del coinflips[(chat_id, msg_id)]
    logging.info(f"User {user_id} canceled coinflip in chat {chat_id}, message {msg_id}.")
    await query.edit_message_text(ext="Coinflip cancelled")

async def get_db_connection():
    return await asyncpg.connect(
        host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )

async def start(update: Update, context: CallbackContext):
    """Handles the /start command by showing available commands."""
    help_text = (
        "🎲 *Welcome to Coinflipper!* 🎲\n\n"
        "This bot helps you manage Bitcoin transactions. Here are the available commands:\n\n"
        "💰 `/balance` – Check your Bitcoin balance\n"
        "🏠 `/address` – Get a new Bitcoin deposit address\n"
        "📤 `/withdraw <address> <amount_in_sats>` – Withdraw Bitcoin to an external address\n\n"
        "🐬 `/coinflip <sats> <number of participants>` – Start coinflip, winner takes all\n\n"
        "🔗 *Source Code:* [GitHub Repository](https://github.com/fridokus/coinflipper)\n\n"
        "⚠  *NOTE:* This bot is super unstable and any funds sent in will possibly, and even probably, get lost forever. Use at your own risk and with small amounts..\n\n"
        "Have fun flipping coins! 🚀"
    )

    await update.message.reply_text(help_text, parse_mode="Markdown")

async def address(update: Update, context: CallbackContext):
    """Handles the /address command by generating a new BTC address if the user has not exceeded the limit."""
    user_id = update.effective_user.id
    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

    conn = await get_db_connection()

    # Count existing addresses for the user
    address_count = await conn.fetchval(
        "SELECT COUNT(*) FROM addresses WHERE user_id = $1", user_id
    )

    if address_count >= 100:
        logging.warning(f"User {user_id} attempted to generate more than 100 addresses.")
        await update.message.reply_text("You have already generated 100 addresses. Limit reached.")
        await conn.close()
        return

    # Generate a new address
    new_address = rpc.getnewaddress(f"user_{user_id}")

    # Ensure the user has an account in the DB
    await conn.execute(
        "INSERT INTO balances (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )

    # Store the generated address
    await conn.execute(
        "INSERT INTO addresses (user_id, address) VALUES ($1, $2)",
        user_id, new_address
    )

    await conn.close()
    logging.info(f"User {user_id} generated a new address: {new_address}")

    await update.message.reply_text(f"Your Bitcoin address: {new_address}")

async def addresses(update: Update, context: CallbackContext):
    """Handles the /addresses command, listing all addresses the user has generated."""
    user_id = update.effective_user.id

    conn = await get_db_connection()
    rows = await conn.fetch("SELECT address FROM addresses WHERE user_id = $1", user_id)
    await conn.close()

    if not rows:
        await update.message.reply_text("You have not generated any addresses yet.")
        return

    # Convert list of addresses into a formatted message
    address_list = "\n".join([row["address"] for row in rows])
    response = f"Your generated addresses:\n{address_list}"

    await update.message.reply_text(response)

def get_user_balance(user_id, conn):
    return conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)

def to_sats(balance):
    return int(balance * 100_000_000)

async def balance(update: Update, context: CallbackContext):
    """Handles the /balance command"""
    user = update.effective_user
    user_id = user.id
    username = user.username if user.username else user.full_name

    conn = await get_db_connection()
    balance = await get_user_balance(user_id, conn)
    await conn.close()

    if balance is None:
        logging.info(f"User {user_id} ({username}) checked balance: No balance found.")
        await update.message.reply_text(f"{username}, you have no balance yet.")
    else:
        logging.info(f"User {user_id} ({username}) checked balance: {balance} BTC.")
        await update.message.reply_text(f"{username}, your balance is {to_sats(balance)} sats 💷")

async def withdraw(update: Update, context: CallbackContext):
    """Handles the /withdraw command to send BTC, ensuring the amount includes the fee."""
    user_id = update.effective_user.id

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /withdraw <address> <amount_in_sats>")
        return

    withdraw_address = context.args[0]
    total_sats = int(context.args[1])  # User-specified amount in satoshis
    total_btc = Decimal(total_sats) / Decimal(100_000_000)  # Convert to BTC

    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)

    if balance is None or balance < total_btc:
        logging.warning(f"User {user_id} attempted to withdraw {total_btc} BTC but has insufficient balance.")
        await update.message.reply_text("Insufficient balance.")
        await conn.close()
        return

    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

    try:
        utxos = rpc.listunspent(1, 9999999, [])
        selected_utxos = []
        total_input = Decimal(0)

        for utxo in utxos:
            if total_input >= total_btc:
                break
            selected_utxos.append(utxo)
            total_input += Decimal(utxo["amount"])

        if total_input < total_btc:
            logging.warning(f"User {user_id} has insufficient confirmed UTXOs for withdrawal.")
            await update.message.reply_text("Not enough confirmed UTXOs.")
            await conn.close()
            return

        inputs = [{"txid": utxo["txid"], "vout": utxo["vout"]} for utxo in selected_utxos]
        outputs = {withdraw_address: 0}  # Placeholder

        # Create a raw transaction to estimate fee
        raw_tx = rpc.createrawtransaction(inputs, outputs)
        estimated_size = len(rpc.decoderawtransaction(raw_tx)["hex"]) // 2  # Convert hex length to bytes
        fee_sats = estimated_size * 2  # Assuming 2 sat/vB fee rate
        fee_btc = Decimal(fee_sats) / Decimal(100_000_000)

        # Ensure user-specified amount is greater than the fee
        if total_btc <= fee_btc:
            logging.warning(f"User {user_id} tried withdrawing {total_btc} BTC, but fee ({fee_btc} BTC) is too high.")
            await update.message.reply_text("Amount too small after fees.")
            await conn.close()
            return

        # Adjust the withdrawal amount (subtract the fee from total)
        withdraw_btc = total_btc - fee_btc
        outputs[withdraw_address] = float(withdraw_btc)

        # Create and send transaction
        raw_tx = rpc.createrawtransaction(inputs, outputs)
        signed_tx = rpc.signrawtransactionwithwallet(raw_tx)
        txid = rpc.sendrawtransaction(signed_tx["hex"])

    except Exception as e:
        logging.error(f"Error during withdrawal for user {user_id}: {e}")
        await update.message.reply_text(f"Error sending BTC: {str(e)}")
        await conn.close()
        return

    # Deduct full amount (user-specified) from balance
    await conn.execute(
        "UPDATE balances SET balance = balance - $1 WHERE user_id = $2", total_btc, user_id
    )
    await conn.close()

    logging.info(f"User {user_id} withdrew {withdraw_btc} BTC to {withdraw_address}. Fee: {fee_btc} BTC. TXID: {txid}")
    await update.message.reply_text(
        f"Sent {int(withdraw_btc * 100_000_000)} sats to {withdraw_address}!\n"
        f"Fee: {fee_sats} sats\n"
        f"Total deducted: {total_sats} sats\n\n"
        f"Transaction ID: {txid}"
    )

def main():
    """Starts the bot"""
    with open('.token', 'r') as f:
        token = f.read().strip()

    logging.info("Starting Telegram bot...")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("address", address))
    app.add_handler(CommandHandler("addresses", addresses))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("coinflip", coinflip))
    app.add_handler(CallbackQueryHandler(join_coinflip, pattern="^join_"))
    app.add_handler(CallbackQueryHandler(cancel_coinflip, pattern="^cancel_"))
    app.run_polling()

if __name__ == "__main__":
    main()

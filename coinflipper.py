#!/usr/bin/python3

import asyncpg
import logging
import random
from bitcoinrpc.authproxy import AuthServiceProxy
from bitcoinrpc.authproxy import JSONRPCException
from decimal import Decimal
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
)

LOG_FILE = "/var/log/coinflipper.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

RPC_USER = "rpcuser"
RPC_PASSWORD = "123"
RPC_HOST = "127.0.0.1"
RPC_PORT = 8332

DB_HOST = "127.0.0.1"
DB_NAME = "coinflipper"
DB_USER = "botuser"
DB_PASSWORD = "123"

coinflips = {}

with open('trivia.txt', 'r') as f:
    TRIVIA = f.read().splitlines()

async def coinflip(update: Update, context: CallbackContext):
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /coinflip <sats> <number of participants>"
        )
        return

    sats = int(context.args[0])
    n_participants = int(context.args[1])
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name
    chat_id = update.message.chat_id
    message = update.message

    logging.info(
        f"User {user_id} ({username}) initiated coinflip: entry={sats} sats, n_participants={n_participants} in chat {chat_id}."
    )

    if n_participants < 2:
        await update.message.reply_text("Need >=2 participants.")
        return

    conn = await get_db_connection()
    balance = await conn.fetchval(
        "SELECT balance FROM balances WHERE user_id = $1", user_id
    )
    await conn.close()

    if balance is None or balance < sats:
        logging.info(
            f"User {user_id} ({username}) has insufficient balance ({balance}) for coinflip entry of {sats} sats."
        )
        await update.message.reply_text(
            "You don't have enough balance to start this coinflip."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "Join", callback_data=f"join_{chat_id}_{message.message_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "Cancel", callback_data=f"cancel_{chat_id}_{message.message_id}"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        f"🎲 Coinflip started! {sats} sats entry. {n_participants} players needed.",
        reply_markup=reply_markup,
    )

    coinflips[(chat_id, message.message_id)] = {
        "creator": user_id,
        "sats": sats,
        "max": n_participants,
        "participants": [],
        "start_time": datetime.utcnow(),
    }

    logging.info(
        f"Coinflip created by user {user_id} ({username}) with message_id {msg.message_id} in chat {chat_id}."
    )


async def join_coinflip(update: Update, context: CallbackContext):
    logging.info(coinflips)
    query = update.callback_query
    _, chat_id, msg_id = query.data.split("_")
    chat_id, msg_id = int(chat_id), int(msg_id)
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.full_name

    logging.info(
        f"User {user_id} ({username}) attempting to join coinflip in chat {chat_id}, message {msg_id}."
    )

    if (chat_id, msg_id) not in coinflips:
        logging.warning(
            f"User {user_id} ({username}) attempted to join a non-existent coinflip in chat {chat_id}, message {msg_id}."
        )
        await query.answer("This coinflip no longer exists.")
        return

    coinflip = coinflips[(chat_id, msg_id)]

    if datetime.utcnow() - coinflip["start_time"] > timedelta(days=1):
        logging.info(
            f"Coinflip in chat {chat_id}, message {msg_id} timed out. Canceling coinflip."
        )
        del coinflips[(chat_id, msg_id)]
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text="Coinflip canceled due to timeout."
        )
        return

    if user_id in [p[0] for p in coinflip["participants"]]:
        logging.info(
            f"User {user_id} ({username}) already joined coinflip in chat {chat_id}, message {msg_id}."
        )
        await query.answer("You have already joined.")
        return

    conn = await get_db_connection()
    balance = await conn.fetchval(
        "SELECT balance FROM balances WHERE user_id = $1", user_id
    )
    await conn.close()
    if balance is None or balance < coinflip["sats"]:
        logging.info(
            f"User {user_id} ({username}) has insufficient balance ({balance}) to join coinflip requiring {coinflip['sats']} sats."
        )
        await query.answer("You don't have enough balance.")
        return

    coinflip["participants"].append((user_id, username))
    logging.info(
        f"User {user_id} ({username}) successfully joined coinflip in chat {chat_id}, message {msg_id}. Total participants: {len(coinflip['participants'])}."
    )

    participant_list = "\n".join([p[1] for p in coinflip["participants"]])
    keyboard = [
        [InlineKeyboardButton("Join", callback_data=f"join_{chat_id}_{msg_id}")],
        [InlineKeyboardButton("Cancel", callback_data=f"cancel_{chat_id}_{msg_id}")],
    ]
    await query.edit_message_text(
        text=f"🎲 Coinflip started! {coinflip['sats']} sats entry. {coinflip['max']} players needed.\n\nParticipants:\n{participant_list}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    if len(coinflip["participants"]) >= coinflip["max"]:
        logging.info(
            f"Coinflip in chat {chat_id}, message {msg_id} reached max participants. Determining winner..."
        )
        for user in coinflip["participants"]:
            user_id = user[0]
            balance = await get_user_balance(user_id)
            if balance < coinflip["sats"]:
                logging.warning(f"Some participants lack funds")
                await query.edit_message_text(text=f"😳 Users lack balance to flip")
                return

        logging.info(f"All participants have sufficient balance. Determining winner...")
        winner = random.choice(coinflip["participants"])
        winner_id, winner_name = winner
        total_prize = coinflip["sats"] * (coinflip["max"] - 1)

        conn = await get_db_connection()
        async with conn.transaction():
            for participant_id, _ in coinflip["participants"]:
                if participant_id != winner_id:
                    await conn.execute(
                        "UPDATE balances SET balance = balance - $1 WHERE user_id = $2",
                        coinflip["sats"],
                        participant_id,
                    )
            await conn.execute(
                "UPDATE balances SET balance = balance + $1 WHERE user_id = $2",
                total_prize,
                winner_id,
            )
        await conn.close()

        logging.info(
            f"Coinflip in chat {chat_id}, message {msg_id}: Winner is user {winner_id} ({winner_name}) winning {total_prize} sats."
        )
        emoji = random.choice([
            "🔥", "🎉", "🥂", "💹", "🦈", "🗽", "🏆", "🏅", "🥇", "💰", "💎", "🎖️", "🚀", "⚡",
            "👑", "🤴", "👸", "🤑", "🎊", "🎯", "🏁", "🦅", "🦾", "💪", "🤩", "🥶",
            "🥵", "💥", "✨", "🌟", "🌠", "🎇", "🎆", "🎵", "🎶", "🎷", "🎺", "🥁", "🕺",
            "💃", "🎭", "🏹", "🛡️", "🗡️", "⚔️", "🧨", "💡", "🔮", "🛸", "🚁", "🌋", "🌊",
            "⏳", "⌛", "🏔️", "🏄", "⛷️", "🏋️", "🤼", "🥋", "🥊", "🤺", "🎿", "🏇", "🎠",
            "🐉", "🐲", "🦄", "🐅", "🐆", "🦁", "🐘", "🐬", "🦈", "🦅", "🦚", "🐓", "🦜",
            "🌞", "🌅", "🌄", "🎑", "🚨", "💣", "📯", "🔊", "📢", "📣", "🎙️", "🎚️", "🎛️",
            "🎚️", "📻", "📡", "🛰️", "💈", "🔱", "🏵️", "🧧", "🎗️", "🎟️"
        ])
        await query.edit_message_text(text=f"{emoji} {winner_name} won the coinflip and received {total_prize} sats!\n\nParticipants:\n{participant_list}",
                reply_markup=None)
        del coinflips[(chat_id, msg_id)]


async def cancel_coinflip(update: Update, context: CallbackContext):
    query = update.callback_query
    _, chat_id, msg_id = query.data.split("_")
    chat_id, msg_id = int(chat_id), int(msg_id)
    user_id = query.from_user.id

    logging.info(
        f"User {user_id} requested cancellation of coinflip in chat {chat_id}, message {msg_id}."
    )

    if (chat_id, msg_id) not in coinflips:
        logging.warning(
            f"User {user_id} attempted to cancel a non-existent coinflip in chat {chat_id}, message {msg_id}."
        )
        await query.answer("This coinflip no longer exists.")
        return

    coinflip = coinflips[(chat_id, msg_id)]
    if user_id != coinflip["creator"]:
        logging.info(
            f"User {user_id} is not the creator and cannot cancel coinflip in chat {chat_id}, message {msg_id}."
        )
        await query.answer("Only the creator can cancel.")
        return

    del coinflips[(chat_id, msg_id)]
    logging.info(
        f"User {user_id} canceled coinflip in chat {chat_id}, message {msg_id}."
    )
    await query.edit_message_text(text="Coinflip cancelled 🌠")


async def get_db_connection():
    return await asyncpg.connect(
        host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


async def trivia(update: Update, context: CallbackContext):
    trivia_text = random.choice(TRIVIA)
    await update.message.reply_text(trivia_text, parse_mode="Markdown")

async def start(update: Update, context: CallbackContext):
    """Handles the /start command by showing available commands."""
    help_text = (
        "🎲 *Welcome to Coinflipper!* 🎲\n\n"
        "This bot helps you manage Bitcoin transactions. Here are the available commands:\n\n"
        "💰 `/balance` – Check your Bitcoin balance\n"
        "🏠 `/address` – Get a new Bitcoin deposit address\n"
        "🏘 `/addresses` – List generated addresses\n"
        "📤 `/withdraw <address> <amount_in_sats>` – Withdraw Bitcoin to an external address\n"
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
        logging.warning(
            f"User {user_id} attempted to generate more than 100 addresses."
        )
        await update.message.reply_text(
            "You have already generated 100 addresses. Limit reached."
        )
        await conn.close()
        return

    new_address = rpc.getnewaddress(f"user_{user_id}")
    await conn.execute(
        "INSERT INTO balances (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id,
    )
    await conn.execute(
        "INSERT INTO addresses (user_id, address) VALUES ($1, $2)", user_id, new_address
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

    address_list = "\n".join([row["address"] for row in rows])
    logging.info(f"User {user_id} checked addresses:\n{address_list}")
    response = f"Your generated addresses:\n{address_list}"

    await update.message.reply_text(response)


async def balance(update: Update, context: CallbackContext):
    """Handles the /balance command"""
    user = update.effective_user
    user_id = user.id
    username = user.username if user.username else user.full_name
    balance = await get_user_balance(user_id)

    if balance is None:
        logging.info(f"User {user_id} ({username}) checked balance: No balance found.")
        await update.message.reply_text(f"{username}, you have no balance yet.")
    else:
        logging.info(f"User {user_id} ({username}) checked balance: {balance} sats.")
        await update.message.reply_text(
            f"{username}, your balance is {balance} sats 💷"
        )


def select_utxos(rpc, amount_btc):
    utxos = rpc.listunspent(1, 9999999, [])
    selected, total_input = [], Decimal(0)

    for utxo in utxos:
        if total_input >= amount_btc:
            break
        selected.append(utxo)
        total_input += Decimal(utxo["amount"])

    return selected, total_input

async def get_user_balance(user_id: int) -> int:
    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    await conn.close()
    return balance

async def update_balance(user_id: int, amount: int):
    conn = await get_db_connection()
    await conn.execute("UPDATE balances SET balance = balance - $1 WHERE user_id = $2", amount, user_id)
    await conn.close()

async def withdraw(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    if len(context.args) < 2 or len(context.args) > 3:
        await update.message.reply_text(
            "❌ *Usage:* `/withdraw <address> <amount_in_sats> [fee_rate]`",
            parse_mode="Markdown"
        )
        return

    withdraw_address = context.args[0]
    total_sats = int(context.args[1])
    total_btc = Decimal(total_sats) / Decimal(100_000_000)

    # Parse optional fee_rate, default to 1.8 sat/vB
    fee_rate = Decimal(context.args[2]) if len(context.args) == 3 else Decimal(1.8)

    balance = await get_user_balance(user_id)
    if balance is None or balance < total_sats:
        await update.message.reply_text("⚠️ *Insufficient balance!* Please check your funds. 💰", parse_mode="Markdown")
        return

    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

    try:
        options = {"fee_rate": float(fee_rate)}  # Convert Decimal to float for RPC
        txid = rpc.send([{withdraw_address: float(total_btc)}], None, "unset", None, options)

        # Deduct from user balance
        await update_balance(user_id, total_sats)

        await update.message.reply_text(
            f"✅ *Withdrawal Successful!* 🎉\n"
            f"💸 Sent `{total_sats}` sats to `{withdraw_address}`\n"
            f"💰 *Fee Rate:* `{fee_rate}` sat/vB\n"
            f"🔗 *Transaction ID:* `{txid}`",
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Error during withdrawal for user {user_id}: {e}")
        await update.message.reply_text(f"❌ *Error sending BTC:* `{str(e)}`", parse_mode="Markdown")


async def blockchaininfo(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    logging.info(f"User {user_id} requested blockchain status.")
    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

    try:
        info = rpc.getblockchaininfo()
        block_height = info["blocks"]
        difficulty = info["difficulty"]
        mempool_size = info["mempoolsize"]
        chain = info["chain"]

        await update.message.reply_text(
            f"📊 *Bitcoin Node Status*\n"
            f"🌎 Network: `{chain}`\n"
            f"⛏️ Block Height: `{block_height}`\n"
            f"📈 Difficulty: `{difficulty}`\n"
            f"📬 Mempool Size: `{mempool_size} tx`\n",
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Error fetching blockchain info: {e}")
        await update.message.reply_text("❌ *Error fetching blockchain status!* 🚨", parse_mode="Markdown")


def main():
    """Starts the bot"""
    with open(".token", "r") as f:
        token = f.read().strip()

    logging.info("Starting Telegram bot...")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("address", address))
    app.add_handler(CommandHandler("addresses", addresses))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("coinflip", coinflip))
    app.add_handler(CommandHandler("trivia", trivia))
    app.add_handler(CommandHandler("blockchaininfo", blockchaininfo))
    app.add_handler(CallbackQueryHandler(join_coinflip, pattern="^join_"))
    app.add_handler(CallbackQueryHandler(cancel_coinflip, pattern="^cancel_"))
    app.run_polling()


if __name__ == "__main__":
    main()

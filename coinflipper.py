#!/usr/bin/python3

import asyncpg
import logging
from bitcoinrpc.authproxy import AuthServiceProxy
from bitcoinrpc.authproxy import JSONRPCException
from decimal import Decimal
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext

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


async def balance(update: Update, context: CallbackContext):
    """Handles the /balance command"""
    user = update.effective_user
    user_id = user.id
    username = user.username if user.username else user.full_name

    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    await conn.close()

    if balance is None:
        logging.info(f"User {user_id} ({username}) checked balance: No balance found.")
        await update.message.reply_text(f"{username}, you have no balance yet.")
    else:
        logging.info(f"User {user_id} ({username}) checked balance: {balance} BTC.")
        await update.message.reply_text(f"{username}, your balance is {int(balance * 100_000_000)} sats 💷")

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
    app.run_polling()

if __name__ == "__main__":
    main()

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
    await update.message.reply_text("Welcome to the coinflipper bot 💸")
    logging.info(f"User {update.effective_user.id} started the bot.")

async def address(update: Update, context: CallbackContext):
    """Handles the /address command by generating a new BTC address"""
    user_id = update.effective_user.id
    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

    try:
        address = rpc.getnewaddress(f"user_{user_id}")
    except JSONRPCException as e:
        logging.error(f"Error generating address for user {user_id}: {e}")
        await update.message.reply_text("Error generating address 💀")
        return

    conn = await get_db_connection()
    await conn.execute(
        "INSERT INTO balances (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )
    await conn.close()

    logging.info(f"Generated new address {address} for user {user_id}.")
    await update.message.reply_text(f"Your Bitcoin address 🎲: {address}")

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
    """Handles the /withdraw command to send BTC"""
    user_id = update.effective_user.id

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /withdraw <address> <amount_in_sats>")
        return

    withdraw_address = context.args[0]
    amount_sats = int(context.args[1])
    amount_btc = Decimal(amount_sats) / Decimal(100_000_000)

    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)

    if balance is None or balance < amount_btc:
        logging.warning(f"User {user_id} attempted to withdraw {amount_btc} BTC but has insufficient balance.")
        await update.message.reply_text("Insufficient balance 🧼")
        await conn.close()
        return

    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

    try:
        utxos = rpc.listunspent(1, 9999999, [])
        selected_utxos = []
        total_input = Decimal(0)

        for utxo in utxos:
            if total_input >= amount_btc:
                break
            selected_utxos.append(utxo)
            total_input += Decimal(utxo["amount"])

        if total_input < amount_btc:
            logging.warning(f"User {user_id} has insufficient confirmed UTXOs for withdrawal.")
            await update.message.reply_text("Not enough confirmed UTXOs.")
            await conn.close()
            return

        inputs = [{"txid": utxo["txid"], "vout": utxo["vout"]} for utxo in selected_utxos]
        outputs = {withdraw_address: float(amount_btc)}

        raw_tx = rpc.createrawtransaction(inputs, outputs)
        estimated_size = len(rpc.decoderawtransaction(raw_tx)["hex"]) // 2
        fee_sats = estimated_size * 2
        fee_btc = Decimal(fee_sats) / Decimal(100_000_000)
        total_cost_btc = amount_btc + fee_btc

        if total_cost_btc > balance:
            logging.warning(f"User {user_id} has insufficient funds after fee calculation.")
            await update.message.reply_text("Not enough balance to cover amount + fees.")
            await conn.close()
            return

        outputs[withdraw_address] = float(amount_btc)
        raw_tx = rpc.createrawtransaction(inputs, outputs)
        signed_tx = rpc.signrawtransactionwithwallet(raw_tx)
        txid = rpc.sendrawtransaction(signed_tx["hex"])

    except Exception as e:
        logging.error(f"Error during withdrawal for user {user_id}: {e}")
        await update.message.reply_text(f"Error sending BTC: {str(e)}")
        await conn.close()
        return

    await conn.execute(
        "UPDATE balances SET balance = balance - $1 WHERE user_id = $2", total_cost_btc, user_id
    )
    await conn.close()

    logging.info(f"User {user_id} withdrew {amount_btc} BTC to {withdraw_address}. Fee: {fee_btc} BTC. TXID: {txid}")
    await update.message.reply_text(
        f"Sent {int(amount_btc * 100_000_000)} sats to {withdraw_address}!\n"
        f"Fee: {fee_sats} sats\n"
        f"Total deducted: {int(total_cost_btc * 100_000_000)} sats\n\n"
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
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.run_polling()

if __name__ == "__main__":
    main()

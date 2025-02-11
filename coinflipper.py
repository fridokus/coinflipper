#!/usr/bin/python3

import asyncpg

from bitcoinrpc.authproxy import AuthServiceProxy
from bitcoinrpc.authproxy import JSONRPCException

from decimal import Decimal

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext

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
    await update.message.reply_text("Welcome to the coinflipper bot!")

async def address(update: Update, context: CallbackContext):
    """Handles the /address command by generating a new BTC address"""
    user_id = update.effective_user.id
    rpc = AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")
    address = rpc.getnewaddress(f"user_{user_id}")

    # Ensure the user has an account in the DB
    conn = await get_db_connection()
    await conn.execute(
        "INSERT INTO balances (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )
    await conn.close()

    await update.message.reply_text(f"Your Bitcoin address: {address}")

async def balance(update: Update, context: CallbackContext):
    """Handles the /balance command"""
    user = update.effective_user
    user_id = user.id
    username = user.username if user.username else user.full_name

    conn = await get_db_connection()
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    await conn.close()

    if balance is None:
        await update.message.reply_text(f"{username}, you have no balance yet.")
    else:
        await update.message.reply_text(f"{username}, your balance is {int(balance * 100_000_000)} sats.")

async def send(update: Update, context: CallbackContext):
    """Handles the /send command to send Bitcoin to another user"""
    user_id = update.effective_user.id
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /send <username> <amount_in_sats>")
        return

    recipient_username = context.args[0].lstrip('@')
    amount_sats = int(context.args[1])
    amount_btc = Decimal(amount_sats) / Decimal(100_000_000)

    conn = await get_db_connection()

    # Find recipient ID
    recipient_id = await conn.fetchval("SELECT user_id FROM balances WHERE user_id IN (SELECT user_id FROM balances WHERE user_id=$1)", recipient_username)
    if not recipient_id:
        await update.message.reply_text(f"User @{recipient_username} not found.")
        await conn.close()
        return

    # Check sender's balance
    sender_balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    if sender_balance is None or sender_balance < amount_btc:
        await update.message.reply_text("Insufficient balance.")
        await conn.close()
        return

    # Update balances
    async with conn.transaction():
        await conn.execute("UPDATE balances SET balance = balance - $1 WHERE user_id = $2", amount_btc, user_id)
        await conn.execute("UPDATE balances SET balance = balance + $1 WHERE user_id = $2", amount_btc, recipient_id)

    await conn.close()
    await update.message.reply_text(f"Sent {amount_sats} sats to @{recipient_username}!")

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

    # Check user's balance
    balance = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    if balance is None or balance < amount_btc:
        await update.message.reply_text("Insufficient balance.")
        await conn.close()
        return

    rpc = get_rpc_connection()

    try:
        # Create a raw transaction to estimate size
        utxos = rpc.listunspent(1, 9999999, [])
        selected_utxos = []
        total_input = Decimal(0)

        for utxo in utxos:
            if total_input >= amount_btc:
                break
            selected_utxos.append(utxo)
            total_input += Decimal(utxo["amount"])

        if total_input < amount_btc:
            await update.message.reply_text("Not enough confirmed UTXOs.")
            await conn.close()
            return

        inputs = [{"txid": utxo["txid"], "vout": utxo["vout"]} for utxo in selected_utxos]
        outputs = {withdraw_address: float(amount_btc)}  # Placeholder output

        raw_tx = rpc.createrawtransaction(inputs, outputs)
        estimated_size = len(rpc.decoderawtransaction(raw_tx)["hex"]) // 2  # Convert hex length to bytes

        # Calculate fee: 2 sat/vB
        fee_sats = estimated_size * 2
        fee_btc = Decimal(fee_sats) / Decimal(100_000_000)

        # Ensure user has enough balance for amount + fee
        total_cost_btc = amount_btc + fee_btc
        if total_cost_btc > balance:
            await update.message.reply_text("Not enough balance to cover amount + fees.")
            await conn.close()
            return

        # Adjust withdrawal amount to subtract the fee
        adjusted_amount_btc = amount_btc
        outputs[withdraw_address] = float(adjusted_amount_btc)

        # Create and send the final transaction
        raw_tx = rpc.createrawtransaction(inputs, outputs)
        signed_tx = rpc.signrawtransactionwithwallet(raw_tx)
        txid = rpc.sendrawtransaction(signed_tx["hex"])

    except Exception as e:
        await update.message.reply_text(f"Error sending BTC: {str(e)}")
        await conn.close()
        return

    # Deduct full amount (including fee) from user's balance
    await conn.execute(
        "UPDATE balances SET balance = balance - $1 WHERE user_id = $2", total_cost_btc, user_id
    )
    await conn.close()

    await update.message.reply_text(
        f"Sent {int(adjusted_amount_btc * 100_000_000)} sats to {withdraw_address}!\n"
        f"Fee: {fee_sats} sats\n"
        f"Total deducted: {int(total_cost_btc * 100_000_000)} sats\n\n"
        f"Transaction ID: {txid}"
    )

def main():
    """Starts the bot"""
    with open('.token', 'r') as f:
        token = f.read().strip()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("address", address))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("send", send))
    app.run_polling()

if __name__ == "__main__":
    main()

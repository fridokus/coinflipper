#!/usr/bin/python3

from bitcoinrpc.authproxy import AuthServiceProxy
from bitcoinrpc.authproxy import JSONRPCException

from decimal import Decimal

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext

RPC_USER = "rpcuser"
RPC_PASSWORD = "123"
RPC_HOST = "127.0.0.1"
RPC_PORT = 8332

def get_rpc_connection():
    return AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

async def address(update: Update, context: CallbackContext):
    """Handles the /address command"""
    user_id = update.effective_user.id
    rpc = get_rpc_connection()
    address = rpc.getnewaddress(f"user_{user_id}")
    await update.message.reply_text(f"New bitcoin address for {user_id}:\n\n{address}")

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("test")

async def balance(update: Update, context: CallbackContext):
    """Handles the /balance command"""
    user = update.effective_user
    user_id = user.id
    username = user.username if user.username else user.full_name
    rpc = get_rpc_connection()
    try:
        addresses = rpc.getaddressesbylabel(f"user_{user_id}")
    except JSONRPCException as e:
        if "No addresses with label" in str(e):
            await update.message.reply_text(f"No addresses found for {username} ({user_id}).")
            return
        else:
            raise e

    total_balance = Decimal(0)
    for address in addresses:
        total_balance += rpc.getreceivedbyaddress(address)

    await update.message.reply_text(f"Balance for {username} ({user_id}): {int(total_balance * 100_000_000)} sats")

def main():
    """Starts the bot"""
    with open('.token', 'r') as f:
        token = f.read().strip()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("address", address))
    app.add_handler(CommandHandler("balance", balance))
    app.run_polling()

if __name__ == "__main__":
    main()

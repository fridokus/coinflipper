#!/usr/bin/python3

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext

with open('.token', 'r') as f:
    TOKEN = f.read().strip()

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("test")

def main():
    """Starts the bot"""
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()

if __name__ == "__main__":
    main()

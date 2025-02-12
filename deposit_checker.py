#!/usr/bin/python3

import asyncio
import asyncpg
import logging
from bitcoinrpc.authproxy import AuthServiceProxy
from decimal import Decimal

# Configure logging
LOG_FILE = "/var/log/deposit_checker.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Bitcoin Core RPC Configuration
RPC_USER = "rpcuser"
RPC_PASSWORD = "123"
RPC_HOST = "127.0.0.1"
RPC_PORT = 8332

# PostgreSQL Configuration
DB_HOST = "127.0.0.1"
DB_NAME = "coinflipper"
DB_USER = "botuser"
DB_PASSWORD = "123"

async def get_db_connection():
    return await asyncpg.connect(
        host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )

def get_rpc_connection():
    return AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")

async def check_deposits():
    """Scans for new deposits and updates user balances"""
    logging.info("Checking for new deposits...")

    try:
        rpc = get_rpc_connection()
        conn = await get_db_connection()

        # Get all unspent transactions
        unspent_txs = rpc.listunspent()

        for tx in unspent_txs:
            txid = tx["txid"]
            vout = tx["vout"]  # Unique per TXID
            address = tx["address"]
            amount = Decimal(tx["amount"])

            # Find which user owns this address
            labels = rpc.getaddressinfo(address).get("labels", [])
            if not labels:
                continue

            label = labels[0]  # Example: "user_123456789"
            if not label.startswith("user_"):
                continue

            user_id = int(label.split("_")[1])

            # Check if this transaction has already been recorded
            tx_exists = await conn.fetchval(
                "SELECT COUNT(*) FROM transactions WHERE txid = $1 AND vout = $2", txid, vout
            )

            if tx_exists: continue

            # Update user's balance
            await conn.execute(
                "UPDATE balances SET balance = balance + $1 WHERE user_id = $2",
                amount,
                user_id,
            )

            # Insert into transactions table to mark it as processed
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, txid, vout) VALUES ($1, 'deposit', $2, $3, $4)",
                user_id, amount, txid, vout
            )

            logging.info(f"Deposited {amount} BTC to user {user_id} (TXID: {txid}, VOUT: {vout})")

        await conn.close()
    except Exception as e:
        logging.error(f"Error in check_deposits: {e}")

async def main():
    while True:
        await check_deposits()
        await asyncio.sleep(20)  # Check every 20 seconds

if __name__ == "__main__":
    logging.info("Starting deposit checker...")
    asyncio.run(main())

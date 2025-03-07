#!/usr/bin/python3

import asyncio
import asyncpg
import logging
from bitcoinrpc.authproxy import AuthServiceProxy
from decimal import Decimal

LOG_FILE = "/var/log/deposit_checker.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
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


def get_rpc_connection():
    return AuthServiceProxy(f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_HOST}:{RPC_PORT}")


async def check_deposits():
    """Scans for new deposits and updates user balances"""

    try:
        rpc = get_rpc_connection()
        conn = await get_db_connection()

        unspent_txs = rpc.listunspent()

        for tx in unspent_txs:
            txid = tx["txid"]
            vout = tx["vout"]
            address = tx["address"]
            amount = int(100_000_000 * Decimal(tx["amount"]))

            labels = rpc.getaddressinfo(address).get("labels", [])
            if not labels:
                continue

            label = labels[0]  # Example: "user_123456789"
            if not label.startswith("user_"):
                continue

            user_id = int(label.split("_")[1])

            tx_exists = await conn.fetchval(
                "SELECT COUNT(*) FROM transactions WHERE txid = $1 AND vout = $2",
                txid,
                vout,
            )

            if tx_exists:
                continue

            await conn.execute(
                "UPDATE balances SET balance = balance + $1 WHERE user_id = $2",
                amount,
                user_id,
            )

            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, txid, vout) VALUES ($1, 'deposit', $2, $3, $4)",
                user_id,
                amount,
                txid,
                vout,
            )

            logging.info(
                f"Deposited {amount} sats to user {user_id} (TXID: {txid}, VOUT: {vout})"
            )

        await conn.close()
    except Exception as e:
        logging.error(f"Error in check_deposits: {e}")


async def main():
    while True == True: # Joke
        logging.info("⏰ Checking 100 times for new deposits...")
        for _ in range(100):
            await check_deposits()
            await asyncio.sleep(60)


if __name__ == "__main__":
    logging.info("Starting deposit checker...")
    asyncio.run(main())

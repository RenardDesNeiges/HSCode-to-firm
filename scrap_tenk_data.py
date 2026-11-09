# %%
import os
import argparse
import sqlite3
import pandas as pd
from tqdm.auto import tqdm      
from concurrent.futures import ProcessPoolExecutor

db_path = 'data/hsmap.db'
chunk_size = 20 
rate_limit = 7

os.environ['EDGAR_RATE_LIMIT_PER_SEC'] = os.environ.get('EDGAR_RATE_LIMIT_PER_SEC', str(rate_limit))

from src.sec_utilities import get_section_10k

def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill tenk_section for filings using SEC 10-K data."
    )
    parser.add_argument(
        '--db-path',
        default=db_path,
        help="Path to the SQLite database file (default: %(default)s).",
    )
    parser.add_argument(
        '--chunk-size',
        type=int,
        default=chunk_size,
        help="Number of rows to process per batch (default: %(default)s).",
    )
    parser.add_argument(
        '--rate-limit',
        type=int,
        default=rate_limit,
        help="Max EDGAR requests per second (default: %(default)s).",
    )
    return parser.parse_args()

def iter_null_batches(conn, chunk=chunk_size):
    """Yield batches of rows where tenk_section IS NULL, newest key ascending."""
    last_key = -1
    while True:
        rows = conn.execute(
            """
            SELECT key, cik, fyear, tenk_section
            FROM filings
            WHERE hs_codes IS NULL AND key > ?
            ORDER BY key
            LIMIT ?
            """,
            (last_key, chunk),
        ).fetchall()
        if not rows:
            break
        last_key = rows[-1][0]          # advance past this batch
        yield rows

def process(row):
    """Stub: return (new_tenk_section, key). Replace with your real logic."""
    key, cik, fyear, _ = row
    out = get_section_10k(cik,fyear)
    if out is None:
        tag = 'Missing 10-K'
    else:
        tag = 'Unprocessed 10-K'
    return (out, tag, key)


def main():
    args = parse_args()
    # Set in the parent before spawning workers so they inherit the value at import time.
    os.environ['EDGAR_RATE_LIMIT_PER_SEC'] = str(args.rate_limit)

    with sqlite3.connect(args.db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE tenk_section IS NULL"
        ).fetchone()[0]

        pbar = tqdm(total=total, unit="row", desc="backfilling tenk_section")

        try:
            with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
                for batch in iter_null_batches(conn, chunk=args.chunk_size):
                    # here the idea is to paralelize the requests
                    updates = list(pool.map(process, batch))
                    conn.executemany(
                        """
                        UPDATE filings SET tenk_section = ?, tag = ? WHERE key = ?
                        """,
                        updates,
                    )
                    conn.commit()
                    pbar.update(len(batch))
        finally:
            pbar.close()


if __name__ == "__main__":
    main()

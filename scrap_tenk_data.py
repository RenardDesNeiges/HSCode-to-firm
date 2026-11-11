# %%
import os
import argparse
import multiprocessing
import sqlite3
import pandas as pd
from tqdm.auto import tqdm      
from concurrent.futures import ProcessPoolExecutor, as_completed

db_path = 'data/hsmap.db'
chunk_size = 128
rate_limit = 9

os.environ['EDGAR_RATE_LIMIT_PER_SEC'] = os.environ.get('EDGAR_RATE_LIMIT_PER_SEC', str(rate_limit))

from src.sec_utilities import (
    SharedRateLimiter,
    configure_worker,
    get_sections_10k_tagged,
    worker_data_directory,
)

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
    """Yield batches of rows that still need a section extraction.

    Rows tagged as permanently unavailable are not retried on every run.
    """
    last_key = -1
    while True:
        rows = conn.execute(
            """
            SELECT key, cik, fyear, tenk_section
            FROM filings
            WHERE tenk_section IS NULL
              AND (tenk_tag IS NULL OR tenk_tag NOT IN ('Missing 10-K', 'No 10-K for fyear', 'No Item 1 Business'))
              AND key > ?
            ORDER BY key
            LIMIT ?
            """,
            (last_key, chunk),
        ).fetchall()
        if not rows:
            break
        last_key = rows[-1][0]          # advance past this batch
        yield rows

def _init_worker(counter, requests_per_second, rate_lock, rate_next):
    """Give each worker its own cache directory and the shared rate limiter."""
    with counter.get_lock():
        index = counter.value
        counter.value += 1
    limiter = SharedRateLimiter(requests_per_second, rate_lock, rate_next)
    configure_worker(worker_data_directory(index), limiter)


def process_cik(cik, rows):
    """rows: list of (key, fyear). Returns list of (tenk_section, tenk_tag, key)."""
    fyears = sorted({r[1] for r in rows})
    try:
        sections = get_sections_10k_tagged(cik, fyears)
    except Exception:
        # Leave the rows NULL so a later rerun retries this CIK.
        return []

    updates = []
    for key, fyear in rows:
        out, tag = sections.get(fyear, (None, 'Document error'))
        updates.append((out, tag, key))
    return updates


def main():
    args = parse_args()
    ctx = multiprocessing.get_context()
    workers = max(1, min(32, os.cpu_count() or 1))
    # Shared by every worker: a counter to name per-worker cache dirs and the
    # state of the global rate limiter.
    worker_counter = ctx.Value('i', 0)
    rate_lock = ctx.Lock()
    rate_next = ctx.Value('d', 0.0, lock=False)

    with sqlite3.connect(args.db_path) as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) FROM filings
            WHERE tenk_section IS NULL
              AND (tenk_tag IS NULL OR tenk_tag NOT IN ('Missing 10-K', 'No 10-K for fyear', 'No Item 1 Business'))
            """
        ).fetchone()[0]

        pbar = tqdm(total=total, unit="row", desc="backfilling tenk_section")

        try:
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=ctx,
                initializer=_init_worker,
                initargs=(worker_counter, args.rate_limit, rate_lock, rate_next),
            ) as pool:
                for batch in iter_null_batches(conn, chunk=args.chunk_size):
                    # one task per CIK, fetching all of its still-pending rows
                    min_key = batch[0][0]
                    ciks = list(dict.fromkeys(row[1] for row in batch))
                    futures = []
                    for cik in ciks:
                        rows = conn.execute(
                            """
                            SELECT key, fyear FROM filings
                            WHERE cik = ? AND tenk_section IS NULL AND key >= ?
                            ORDER BY key
                            """,
                            (cik, min_key),
                        ).fetchall()
                        futures.append(pool.submit(process_cik, cik, rows))

                    updates = []
                    for future in as_completed(futures):
                        updates.extend(future.result())

                    conn.executemany(
                        """
                        UPDATE filings SET tenk_section = ?, tenk_tag = ? WHERE key = ?
                        """,
                        updates,
                    )
                    conn.commit()
                    pbar.update(len(updates))
        finally:
            pbar.close()


if __name__ == "__main__":
    main()

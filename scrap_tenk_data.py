# %%
import os
import sqlite3
import pandas as pd
from tqdm.auto import tqdm      
from concurrent.futures import ProcessPoolExecutor
os.environ['EDGAR_RATE_LIMIT_PER_SEC'] = '7'
from src.sec_utilities import get_section_10k

db_path = 'data/hsmap.db'
chunk_size = 20 

# %%
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


# %%
def main():
    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE tenk_section IS NULL"
        ).fetchone()[0]

        pbar = tqdm(total=total, unit="row", desc="backfilling tenk_section")

        try:
            with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
                for batch in iter_null_batches(conn, chunk=chunk_size):
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


# %%
if __name__ == "__main__":
    main()

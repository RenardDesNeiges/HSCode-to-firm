import argparse
import itertools
import os
import sqlite3
import sys

import pandas as pd
from tqdm import tqdm

DEFAULT_PATH = '~/Documents/Datasets/Compustat/computstat_northamerica_complete_download_24042026.csv'
DEFAULT_DB_PATH = 'data/hsmap.db'
SHORT_LIMIT = 3000

parser = argparse.ArgumentParser(
    description='Initialize the filings SQLite database from a Compustat CSV file.'
)
parser.add_argument(
    'path',
    nargs='?',
    default=DEFAULT_PATH,
    help=f'path to the Compustat CSV file (default: {DEFAULT_PATH})',
)
parser.add_argument(
    'db_path',
    nargs='?',
    default=DEFAULT_DB_PATH,
    help=f'path to the SQLite database to create (default: {DEFAULT_DB_PATH})',
)
parser.add_argument(
    '--short',
    action='store_true',
    help=f'only load the first {SHORT_LIMIT} firm-year entries into the database '
         '(useful for a small debug database)',
)
parser.add_argument(
    '--batch-size',
    type=int,
    default=1000,
    help='number of rows inserted per executemany batch (default: 1000)',
)
args = parser.parse_args()

if args.batch_size < 1:
    parser.error('--batch-size must be a positive integer')

path = os.path.expanduser(args.path)
db_path = os.path.expanduser(args.db_path)

# Never silently overwrite an existing database
if os.path.exists(db_path):
    while True:
        answer = input(
            f'Database file {db_path!r} already exists. '
            'Type "erase" to delete it and start over, or "abort" to stop: '
        ).strip().lower()
        if answer in ('erase', 'abort'):
            break
        print('Please answer "abort" or "erase".')

    if answer == 'abort':
        print(f'Aborted. Existing database {db_path!r} was left untouched.')
        sys.exit(0)

    os.remove(db_path)
    print(f'Erased existing database {db_path!r}')



chunks = pd.read_csv(path, 
                usecols=["cik","at", "sic", "conm", "stko", "fyear","datadate"], 
                dtype={"cik": str, "conm":str, "fyear": int},
                parse_dates=['datadate'], 
                chunksize=10**6)

# we focus on publicly traded firms on a major exchange
# publicly traded firms have stock ownership code 0 (Publicly traded on a major exchange) not 3 (Publicly traded on a minor exchange), 1 and 2 (subsidiaries) and 4 (company involved in an LBO are out of the question)
data = pd.concat([c[c["fyear"].between(2000, 2024) & c['stko'].isin([0.])] for c in chunks])
print(f'{data.shape[0]} firm-year pairs after selecting the right years and filtering to keep only publicly traded firms')

# only accept firm-year pairs with a CIK (i.e. public companies)
data = data[~data['cik'].isna()] 

# Remove duplicate cik/fyear pairs, only keep most recent data
data = data.sort_values(
    ["cik", "fyear", "datadate"],
    ascending=[True, True, True],
)
data = data.drop_duplicates(subset=["cik", "fyear"], keep="last")
print(f'{data.shape[0]} firm-year pairs after removing duplicates')

# Narrow down to large manufacturing firms
data = data[data["at"]>1e3]
data = data[data["sic"].between(1500,3990)]
print(f'{data.shape[0]} firm-year pairs after narrowing down to large manufacturing firms')

if args.short:
    data = data.head(SHORT_LIMIT)
    print(f'{data.shape[0]} firm-year pairs after limiting to the first {SHORT_LIMIT} (--short)')

# Connect (creates the file if it doesn't exist)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# The database is rebuilt from scratch, so durability can be relaxed during the
# bulk load. This avoids an fsync for every batch.
cursor.execute("PRAGMA journal_mode = MEMORY")
cursor.execute("PRAGMA synchronous = OFF")

# Create table with the database schema
cursor.execute("""
CREATE TABLE IF NOT EXISTS filings (
    key             INTEGER PRIMARY KEY AUTOINCREMENT,
    cik             TEXT    NOT NULL,
    conm            TEXT    NOT NULL,
    fyear           TEXT    NOT NULL,
    tenk_section    TEXT,
    tenk_tag        TEXT    NOT NULL DEFAULT 'NO_DATA',
    hs_codes        TEXT,
    hs_output       TEXT    NOT NULL DEFAULT 'NO_DATA',
    hs_tag          TEXT    NOT NULL DEFAULT 'NO_DATA'
);
""")

conn.commit()

# Bulk insert: the whole load runs in a single transaction, batches only drive
# the progress bar and bound memory usage. A single commit at the end is what
# makes this fast (no fsync per row).
rows = zip(data["cik"].astype(str), data["conm"].astype(str),data["fyear"].astype(str))
with tqdm(total=data.shape[0]) as pbar:
    while True:
        batch = list(itertools.islice(rows, args.batch_size))
        if not batch:
            break
        cursor.executemany(
            "INSERT INTO filings (cik, conm, fyear) VALUES (?, ?, ?)",
            batch,
        )
        pbar.update(len(batch))
conn.commit()

# Build the index once, after the load: much cheaper than maintaining it for
# every inserted row. The rows are already unique thanks to drop_duplicates().
cursor.execute("CREATE UNIQUE INDEX idx_cik_fyear ON filings(cik, fyear);")
conn.commit()

conn.close()



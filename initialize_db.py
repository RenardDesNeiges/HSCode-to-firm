import argparse
import itertools
import os
import sqlite3
import sys

import pandas as pd
from tqdm import tqdm

DEFAULT_PATH = '~/Documents/Datasets/Compustat/computstat_northamerica_complete_download_24042026.csv'
DEFAULT_DB_PATH = 'data/hsmap.db'
SHORT_LIMIT = 500
SHORT_SEED = 42

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
    help=f'randomly sample {SHORT_LIMIT} firm-year entries for the database '
         '(useful for a small debug database)',
)
parser.add_argument(
    '--batch-size',
    type=int,
    default=1000,
    help='number of rows inserted per executemany batch (default: 1000)',
)
parser.add_argument(
    '--append',
    action='store_true',
    help='add the firm-year pairs missing from an existing database instead '
         'of recreating it; rows already present are left untouched',
)
parser.add_argument(
    '--min-assets',
    type=float,
    default=1e3,
    metavar='AT',
    help='keep firm-years with total assets (at, $ millions) strictly greater '
         'than AT (default: 1e3)',
)
args = parser.parse_args()

if args.batch_size < 1:
    parser.error('--batch-size must be a positive integer')
if args.append and args.short:
    parser.error('--short cannot be combined with --append')

path = os.path.expanduser(args.path)
db_path = os.path.expanduser(args.db_path)

if args.append:
    if os.path.exists(db_path):
        print(f'Appending to existing database {db_path!r}')
    else:
        print(f'Database {db_path!r} does not exist; creating it')
else:
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
data = data[data["at"] > args.min_assets]
data = data[data["sic"].between(1500,3990)]
print(f'{data.shape[0]} firm-year pairs after narrowing down to large manufacturing firms '
      f'(at > {args.min_assets:g})')

if args.short:
    data = data.sample(n=min(SHORT_LIMIT, len(data)), random_state=SHORT_SEED)
    print(f'{data.shape[0]} firm-year pairs after randomly sampling {SHORT_LIMIT} (--short)')

# Connect (creates the file if it doesn't exist)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

if args.append:
    # Appending must not risk the rows already in the database, so keep
    # journaling enabled and commit after every batch (see below).
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
else:
    # The database is rebuilt from scratch, so durability can be relaxed during
    # the bulk load. This avoids an fsync for every batch.
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

if args.append:
    # Skip the firm-years already in the database so their processed columns
    # (tenk_section, hs_codes, ...) are preserved exactly as they are.
    existing = {
        f'{cik}|{fyear}'
        for cik, fyear in cursor.execute("SELECT cik, fyear FROM filings")
    }
    data_keys = (
        data["cik"].astype(str) + "|" + data["fyear"].astype(int).astype(str)
    )
    new_rows = ~data_keys.isin(existing)
    print(f'{int(new_rows.sum())} new firm-year pairs to add, '
          f'{int((~new_rows).sum())} already present and left untouched')
    data = data[new_rows]

# Bulk insert: the whole load runs in a single transaction, batches only drive
# the progress bar and bound memory usage. A single commit at the end is what
# makes this fast (no fsync per row). When appending, each batch is committed
# separately so an interruption keeps everything added so far, and OR IGNORE
# guarantees that an existing row can never be overwritten.
insert_sql = (
    "INSERT OR IGNORE INTO filings (cik, conm, fyear) VALUES (?, ?, ?)"
    if args.append
    else "INSERT INTO filings (cik, conm, fyear) VALUES (?, ?, ?)"
)
changes_before = conn.total_changes
rows = zip(data["cik"].astype(str), data["conm"].astype(str),data["fyear"].astype(str))
with tqdm(total=data.shape[0]) as pbar:
    while True:
        batch = list(itertools.islice(rows, args.batch_size))
        if not batch:
            break
        cursor.executemany(
            insert_sql,
            batch,
        )
        if args.append:
            conn.commit()
        pbar.update(len(batch))
conn.commit()

# Build the index once, after the load: much cheaper than maintaining it for
# every inserted row. The rows are already unique thanks to drop_duplicates().
# IF NOT EXISTS keeps an existing database usable when appending to it.
cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cik_fyear ON filings(cik, fyear);")
conn.commit()

if args.append:
    print(f'Inserted {conn.total_changes - changes_before} new firm-year pairs')

total = cursor.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
print(f'Database {db_path!r} now holds {total} firm-year pairs')

conn.close()



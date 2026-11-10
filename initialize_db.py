import argparse
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
args = parser.parse_args()

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
                 usecols=["cik", "fyear","datadate"], 
                 dtype={"cik": str, "fyear": int},
                 parse_dates=['datadate'], 
                 chunksize=10**6)

data = pd.concat([c[c["fyear"].between(2000, 2024)] for c in chunks])
print(f'{data.shape[0]} firm-year pairs after selecting the right years')

# only accept firm-year pairs with a CIK (i.e. public companies)
data = data[~data['cik'].isna()] 
print(f'{data.shape[0]} firm-year pairs after removing private firms')

# Remove duplicate cik/fyear pairs, only keep most recent data
data = data.sort_values(
    ["cik", "fyear", "datadate"],
    ascending=[True, True, True],
)
data = data.drop_duplicates(subset=["cik", "fyear"], keep="last")
print(f'{data.shape[0]} firm-year pairs after removing duplicates')

if args.short:
    data = data.head(SHORT_LIMIT)
    print(f'{data.shape[0]} firm-year pairs after limiting to the first {SHORT_LIMIT} (--short)')

# Connect (creates the file if it doesn't exist)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create table with the database schema
cursor.execute("""
CREATE TABLE IF NOT EXISTS filings (
    key             INTEGER PRIMARY KEY AUTOINCREMENT,
    cik             TEXT    NOT NULL,
    fyear           TEXT    NOT NULL,
    tenk_section    TEXT,
    tenk_tag        TEXT    NOT NULL DEFAULT 'NO_DATA',
    hs_codes        TEXT,
    hs_output       TEXT    NOT NULL DEFAULT 'NO_DATA',
    hs_tag          TEXT    NOT NULL DEFAULT 'NO_DATA'
);
""")

# Helpful indexes for common lookups
cursor.execute("CREATE UNIQUE INDEX idx_cik_fyear ON filings(cik, fyear);")

conn.commit()

for i in tqdm(range(data.shape[0])):
    row = data.iloc[i]
    cursor.execute(
        "INSERT INTO filings (cik, fyear) VALUES (?, ?)",
        (row['cik'], str(row['fyear'])),
    )
    conn.commit()



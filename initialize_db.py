import argparse
import os
import sqlite3
import sys

import pandas as pd
from tqdm import tqdm

DEFAULT_PATH = '~/Documents/Datasets/Compustat/computstat_northamerica_complete_download_24042026.csv'
DEFAULT_DB_PATH = 'data/hsmap.db'

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
args = parser.parse_args()

path = args.path
db_path = args.db_path

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

data = pd.read_csv(path)

print(f'{data.shape[0]} firm-year pairs without filtering')

# only accept firm-year pairs between 2000 and 2024
data = data[(data['fyear']>1999) & (data['fyear']<2025)]
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

# Connect (creates the file if it doesn't exist)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create table with the database schema
cursor.execute("""
CREATE TABLE IF NOT EXISTS filings (
    key        INTEGER PRIMARY KEY AUTOINCREMENT,
    cik        TEXT    NOT NULL,
    fyear      TEXT    NOT NULL,
    tenk_section TEXT,
    hs_codes   TEXT
);
""")

# Helpful indexes for common lookups
cursor.execute("CREATE UNIQUE INDEX idx_cik_fyear ON filings(cik, fyear);")

conn.commit()

for i in tqdm(range(data.shape[0])):
    row = data.iloc[i]
    cursor.execute(
        "INSERT INTO filings (cik, fyear, tenk_section, hs_codes) VALUES (?, ?, ?, ?)",
        (str(int(row['cik'])), str(row['fyear']), None, None),
    )
    conn.commit()



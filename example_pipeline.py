# %%
import itertools
import sqlite3
import pandas as pd
from tqdm import tqdm
from src.llm_utilities import secure_sec_length, parse_filing, parse_filing_context, generate_search_terms, parse_product_hs
from src.sec_product_scrapper import get_first_guess_list, refine_product_list

from src.semantic_search import SemanticSearch
semsearch = SemanticSearch()

DB_PATH = "data/hsmap.db"

# Get one row from the database (this is just to have an example tenk entry)
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("SELECT key, conm, cik, fyear, tenk_section FROM filings")
data = [row[1:] for row in cursor.fetchall()]
data = pd.DataFrame(data,columns=["conm", "cik", "fyear", "tenk_section"])
conm, cik, fyear, tenk_section = data.iloc[0]

# Run the pipeline

company = {}

print("parsing product list")
tenk = secure_sec_length(tenk_section)
products = parse_filing(tenk)
products

print("parsing context")
context = parse_filing_context(conm)
company['context'] = context
context

productlist = products.split(',')
productlist

pdata = {}
for p in productlist:
    item = {}
    print(f"Product : {p}")
    item['search_terms'] = generate_search_terms(p)
    print(f'Search terms: {item['search_terms']}')

    item['candidates'] = semsearch.get_candidates(p + '\n' \
                            + item['search_terms'], candidate_count=20)
    print(f'Candidates found')
    item['result'] = parse_product_hs(item['candidates']\
                                        .to_string(index=False),p, context)


    print(f'Results = {item['result']}')
    pdata[p] = item

print("refining")

company['product data'] = pdata

code_probabilities = {}
for item in pdata.values():
    for code in item['result'].codes:
        code_probabilities[code] = max(
            code_probabilities.get(code, 0.0),
            item['result'].probabilities.get(code, 0.0),
        )
company['hs_probabilities'] = sorted(code_probabilities.items())

first_guess_list = get_first_guess_list(company)

print(first_guess_list)

company['ig_codes'] = first_guess_list
to_remove = refine_product_list(first_guess_list,context)
company['removed'] = to_remove
print(to_remove)
company['hs_probabilities'] = [
    (code, probability)
    for code, probability in company['hs_probabilities']
    if code not in to_remove
]


print("\nDONE!\n")

print(company)
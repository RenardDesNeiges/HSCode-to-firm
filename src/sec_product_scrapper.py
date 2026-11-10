import pandas as pd
from src.sec_utilities import get_section_10k
from src.llm_utilities import parse_filing, parse_product_hs, parse_filing_context, generate_search_terms, refine_product_list, secure_sec_length
from src.semantic_search import SemanticSearch
from tqdm import tqdm
from datetime import datetime
semsearch = SemanticSearch()
import itertools
import traceback

hscodes = pd.read_excel("data/HSCodeandDescription.xlsx",sheet_name='HS22')
hscodes4digits = hscodes[hscodes['Level']==4]
hscodes4digits = hscodes4digits[['Code','Description']]
hscodes4digits.index = hscodes4digits['Code']

def log_error(error_log_path,e,cik):
    with open(error_log_path, 'a') as err_f:
        err_f.write(f"--- {datetime.now()} ---\n")
        err_f.write(f"CIK: {cik}\n")
        err_f.write(f"Error type: {type(e).__name__}\n")
        err_f.write(f"Error message: {e}\n")
        err_f.write("Traceback:\n")
        err_f.write(traceback.format_exc())
        err_f.write("\n")

def parse_tick_data(data):
    codes = set().union(*[set(data['product data'][p]['result'].replace(' ','').split(',')) for p in data['product data'].keys()])
    codes = {code for code in codes if code.isdigit()}
    return hscodes4digits.loc[list(set(hscodes4digits['Code']).intersection(codes))]

def get_first_guess_list(company):
    out = ""
    for _, row in parse_tick_data(company).iterrows():
        out += row['Code'] + " : " + row["Description"] + "\n"
    return out

def get_hs_code_trace(data):
    out = {}
    for k,v in data['product data'].items():
        for hs in v['result'].replace(' ','').split(','):
            if hs not in out:
                out[hs] = {k.strip()}
            else:
                out[hs].add(k.strip())
    return out

def get_tracable_array(data):
    solution = parse_tick_data(data)\
        [~parse_tick_data(data)['Code'].isin(data['removed'])]
    solution['Source'] = solution['Code']\
        .apply(lambda x : ', '.join(get_hs_code_trace(data)[x]))
    return solution

def ticker_data_to_str(ticker_data):
    out = f"{ticker_data['ticker']} | "
    output_data = get_tracable_array(ticker_data)
    for _,row in output_data.iterrows():
        out += f"{row['Code']} : {row['Source']}; "
    out += f' | ' + datetime.now().strftime('%Y-%m-%d/%H-%M-%S') + '\n'
    return out

def parse_tenk(cik:str, tenk:str,pbar):
    company = {}

    tenk = secure_sec_length(tenk)
    
    pbar.set_description(f'Generating a product '
                         f'list for company {cik}')
    products = parse_filing(tenk)

    pbar.set_description(f'Generating context '
                         f'for company {cik}')
    context = parse_filing_context(cik)

    company['context'] = context

    productlist = products.split(',')
    pdata = {}
    for p in productlist:
        item = {}

        pbar.set_description(f'Generating search terms ' 
                             f'for product {p} of company {cik}')
        item['product'] = p
        item['search_terms'] = generate_search_terms(p)


        pbar.set_description(f'Searching HS candidates '
                             f'for product {p} of company {cik}')
        item['candidates'] = semsearch.get_candidates(p + '\n' \
                                + item['search_terms'], candidate_count=20)

        pbar.set_description(f'Reranking HS candidates '
                             f'for product {p} of company {cik}')
        item['result'] = parse_product_hs(item['candidates']\
                                            .to_string(index=False),p, context)
        pdata[p] = item
    pbar.set_description(f'Refining initial guess '
                        f'for company {cik}')
    company['product data'] = pdata
    first_guess_list = get_first_guess_list(company)
    company['ig_codes'] = first_guess_list
    to_remove = refine_product_list(first_guess_list,context)
    company['removed'] = to_remove

    codes = set(itertools.chain.from_iterable(
        [pdata[k]['result'].replace(' ','').split(',') for k in pdata.keys()]
    )) - company['removed']
    company['codes'] = codes
    return company

def get_product_list(ciklist, output_path, error_log_path):

    with tqdm(total=len(ciklist), 
            desc="Processing CIKs") as pbar:
        data = {}
        for cik in ciklist:
            try:
                cik_data = parse_tenk(cik,pbar)
                str_buffer = ticker_data_to_str(cik_data)
                with open(output_path, 'a') as f:
                    f.write(str_buffer)
                data[cik] = cik_data
            except Exception as e:
                with open(output_path, 'a') as f:
                    f.write(f"{cik} | ERROR \n")
                        # Write detailed error info to the separate error file
                log_error(error_log_path, e, cik)
            pbar.update(1)

    return data

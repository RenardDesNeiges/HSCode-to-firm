import pandas as pd
from src.sec_utilities import get_section_10k
from src.llm_utilities import parse_filing, parse_product_hs, parse_filing_context, generate_search_terms, refine_product_list, secure_sec_length
from src.semantic_search import SemanticSearch
from tqdm import tqdm
from datetime import datetime
semsearch = SemanticSearch()
import traceback

hscodes = pd.read_excel("data/HSCodeandDescription.xlsx",sheet_name='HS22')
hscodes4digits = hscodes[hscodes['Level']==4]
hscodes4digits = hscodes4digits[['Code','Description']]
hscodes4digits.index = hscodes4digits['Code']

def log_error(error_log_path,e,ticker):
    with open(error_log_path, 'a') as err_f:
        err_f.write(f"--- {datetime.now()} ---\n")
        err_f.write(f"Ticker: {ticker}\n")
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

def parse_ticker(ticker,pbar,cik):

    company = {}
    company['ticker'] = ticker
    pbar.set_description(f'Scrapping the filings '
                         f'of company {ticker}')
    filing = get_section_10k(ticker,cik=cik)
    filing = secure_sec_length(filing)
    
    pbar.set_description(f'Generating a product '
                         f'list for company {ticker}')
    products = parse_filing(filing)

    pbar.set_description(f'Generating context '
                         f'for company {ticker}')
    context = parse_filing_context(filing)

    company['filing'] = filing
    company['context'] = context

    productlist = products.split(',')
    product_data = {}
    for p in productlist:
        item = {}

        pbar.set_description(f'Generating search terms ' 
                             f'for product {p} of company {ticker}')
        item['product'] = p
        item['search_terms'] = generate_search_terms(p)


        pbar.set_description(f'Searching HS candidates '
                             f'for product {p} of company {ticker}')
        item['candidates'] = semsearch.get_candidates(p + '\n' \
                                + item['search_terms'], candidate_count=20)

        pbar.set_description(f'Reranking HS candidates '
                             f'for product {p} of company {ticker}')
        item['result'] = parse_product_hs(item['candidates']\
                                            .to_string(index=False),p, context)
        product_data[p] = item
    pbar.set_description(f'Refining initial guess '
                        f'for company {ticker}')
    company['product data'] = product_data
    first_guess_list = get_first_guess_list(company)
    to_remove = refine_product_list(first_guess_list,context,False)
    company['removed'] = to_remove
    return company

def get_product_list(tickerlist, output_path, error_log_path, cik):

    with tqdm(total=len(tickerlist), 
            desc="Processing tickers") as pbar:
        data = {}
        for ticker in tickerlist:
            try:
                ticker_data = parse_ticker(ticker,pbar,cik)
                str_buffer = ticker_data_to_str(ticker_data)
                with open(output_path, 'a') as f:
                    f.write(str_buffer)
                data[ticker] = ticker_data
            except Exception as e:
                with open(output_path, 'a') as f:
                    f.write(f"{ticker} | ERROR \n")
                        # Write detailed error info to the separate error file
                log_error(error_log_path, e, ticker)
            pbar.update(1)

    return data

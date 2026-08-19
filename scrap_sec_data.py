import argparse
import pickle

def scrap(tickerlist,args):
    from src.sec_product_scrapper import get_product_list

    data = get_product_list(tickerlist, args.o, args.e, args.cik)
    with open(args.p, 'wb') as f:
        pickle.dump(data, f)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', default='data/ticker_list.txt',
            help="the list of tickers to be processed in format "
                "TCK1, TCK2, ...")
    parser.add_argument('--cik', action='store_true', help='if selected reads CIK instead of tickers')
    parser.add_argument('-o', default='data/product_data/run.txt',
            help="The ouptut file to store the results in")
    parser.add_argument('-e', default='data/product_data/run_error_log.txt',
            help="The error log file to store the errors in")
    parser.add_argument('-p', default='data/product_data/data_log.txt',
            help="The pickle file to store the full data object in")
    args = parser.parse_args()

    try: 
        with open(args.t, 'r') as f:
            ticker_str = f.read()
            tickerlist = ticker_str.replace(' ','').split(',')
    except:
        print("Invalid ticker-list file")
        return None

    scrap(tickerlist,args)
    


if __name__ == "__main__":
    main()
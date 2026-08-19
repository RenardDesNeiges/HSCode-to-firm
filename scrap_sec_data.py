import argparse
import pickle

def scrap(ciklist,args):
    from src.sec_product_scrapper import get_product_list

    data = get_product_list(ciklist, args.o, args.e)
    with open(args.p, 'wb') as f:
        pickle.dump(data, f)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', default='data/ciks.txt',
            help="the list of CIKs to be processed in format "
                "CIK1, CIK2, ...")
    parser.add_argument('-o', default='data/product_data/run.txt',
            help="The ouptut file to store the results in")
    parser.add_argument('-e', default='data/product_data/run_error_log.txt',
            help="The error log file to store the errors in")
    parser.add_argument('-p', default='data/product_data/data_log.txt',
            help="The pickle file to store the full data object in")
    args = parser.parse_args()

    try: 
        with open(args.t, 'r') as f:
            cik_str = f.read()
            ciklist = cik_str.replace(' ','').split(',')
    except:
        print("Invalid CIK-list file")
        return None

    scrap(ciklist,args)
    


if __name__ == "__main__":
    main()
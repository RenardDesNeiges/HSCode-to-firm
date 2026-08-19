import re
import sys
import os
import importlib.util
import sec2md
from sec_parsers import Filing
from sec_edgar_downloader import Downloader as SEC_Downloader
from edgar import Company, set_identity

# Load SEC identity settings from the repository root config file.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config_spec = importlib.util.spec_from_file_location(
    "config", os.path.join(_ROOT_DIR, "config.py")
)
_config_module = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config_module)

set_identity(_config_module.SEC_EMAIL)
downloader = SEC_Downloader(_config_module.SEC_NAME, _config_module.SEC_EMAIL)
dl = SEC_Downloader("sec_filings", _config_module.SEC_EMAIL)
sys.setrecursionlimit(50000)


def extract_between(text):
    pat = re.compile(r'(?i)\bitem (\d+)\b(?:\.(?!\w))?')
    markers = [(int(m.group(1)), m.start(), m.end()) for m in pat.finditer(text)]
    best, best_len = None, -1
    for i, (num, s1, e1) in enumerate(markers):
        if num == 1:
            for j in range(i+1, len(markers)):
                if markers[j][0] == 2:
                    s2 = markers[j][1]
                    length = s2 - e1
                    if length > best_len:
                        best_len, best = length, text[e1:s2]
                    break  
    return best


def get_business_section_cik(cik):
    filings = Company(cik).get_filings(form="10-K")
    if filings[0].form == '10-K':
        filing = filings[0]
    else:
        filing = filings[1]
    tenk = filing.sections()

    extraction = extract_between(filing.text())
    if extraction:
        return extraction
    else:
        return filing.text()
    
def get_10k(ticker,verbose = False):
    """Reads a 10-K filing directly from SEC EDGAR, given the company's ticker
    """

    folder_path = f"sec-edgar-filings/{ticker}"

    if not os.path.isdir(folder_path):
        if verbose: print('Downloading the 10-k filing')
        dl.get("10-K", ticker, limit=1)
    else:
        if verbose: print('Filing already downloaded')

    filing_path = f"sec-edgar-filings/{ticker}/10-K/"

    for root, _, files in os.walk(filing_path):
        for file in files:
            if file.endswith(".txt"):
                filing_path = os.path.join(root, file)

    with open(filing_path, "r", encoding="utf-8", errors="ignore") as f:
        filing_text = str(f.read()) 
        return filing_text
            
def get_section_10k(identifyer, sec_code = sec2md.Item10K.BUSINESS, verbose=False,cik=False):

    if cik:
        section = get_business_section_cik(identifyer)
    else:
        md_path = f"sec-edgar-filings/{identifyer}/10k_26.md"
        if os.path.isfile(md_path):
            if verbose: 
                print("MD File already exists!")
            with open(md_path, "r", encoding="utf-8") as f:
                return str(f.read()) 

        str_10k_filing = get_10k(identifyer,verbose)

        parser = sec2md.Parser(str_10k_filing)

        pages = parser.get_pages()

        sections = sec2md.extract_sections(pages, filing_type="10-K")
        if len(sections) == 0:
            return ' '.join([p.content for p in pages[:10]])

        biz = sec2md.get_section(sections, sec_code)
        if biz is None:
            section = sections[0].markdown()
        else:
            section = biz.markdown()

        with open(md_path, "w", encoding="utf-8") as f:
            if verbose: 
                print("Writing section to md file!")
            f.write(section)

    return section

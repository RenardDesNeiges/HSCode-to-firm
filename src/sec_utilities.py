import re
import sys
import os
import importlib.util
from edgar import Company, set_identity

# Load SEC identity settings from the repository root config file.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config_spec = importlib.util.spec_from_file_location(
    "config", os.path.join(_ROOT_DIR, "config.py")
)
_config_module = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config_module)

set_identity(_config_module.SEC_EMAIL)
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


def get_section_10k(cik:str,fyear:str):
    filings = Company(cik).get_filings(form="10-K",year=fyear)
    try:
        tenk = filings[0].text()
    except:
        return None

    extraction = extract_between(tenk)
    if extraction:
        return extraction
    else:
        return tenk

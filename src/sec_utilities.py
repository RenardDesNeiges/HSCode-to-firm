import re
import sys
import os
import json
import time
import importlib.util
from html import unescape
from pathlib import Path

from edgar import Company, set_identity
from edgar.config import SEC_ARCHIVE_URL
from edgar.httprequests import download_file

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


class SharedRateLimiter:
    """Paces EDGAR requests across worker processes.

    httpxthrottlecache's transport calls ``try_acquire(name)`` until it returns
    True, so this mirrors that part of the pyrate-limiter API. The state lives in
    multiprocessing primitives shared by every worker, which keeps the overall
    request rate at ``requests_per_second`` instead of multiplying it by the
    number of processes.
    """

    def __init__(self, requests_per_second, lock, next_time):
        self._lock = lock
        self._next_time = next_time
        self._interval = 1.0 / max(1, requests_per_second)

    def try_acquire(self, _name=None):
        # Reserve the next slot while holding the lock, then wait outside it so a
        # worker dying mid-wait cannot wedge the shared lock.
        with self._lock:
            slot = max(time.monotonic(), self._next_time.value)
            self._next_time.value = slot + self._interval
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return True


def worker_data_directory(index: int) -> Path:
    """Return an EDGAR data directory private to one worker process."""
    from edgar.core import get_edgar_data_directory

    base = Path(get_edgar_data_directory())
    return base / "_workers" / f"worker_{index}"


def _prune_corrupt_cache():
    """Drop cache entries left half-written by an interrupted run."""
    from edgar.httpclient import get_cache_directory

    cache_dir = Path(get_cache_directory())
    for meta in cache_dir.rglob('*.meta'):
        try:
            json.loads(meta.read_text())
            continue
        except (OSError, ValueError):
            pass
        for path in (meta, meta.with_suffix('')):
            path.unlink(missing_ok=True)
    for pattern in ('*.tmp', '*.lock'):
        for path in cache_dir.rglob(pattern):
            path.unlink(missing_ok=True)


def configure_worker(data_dir, limiter):
    """Isolate this worker's HTTP cache and install the shared rate limiter."""
    os.environ['EDGAR_LOCAL_DATA_DIR'] = str(data_dir)
    from edgar import httpclient

    previous = httpclient.HTTP_MGR
    manager = httpclient.get_http_mgr()
    manager.rate_limiter = limiter
    httpclient.HTTP_MGR = manager
    previous.close()

    _prune_corrupt_cache()


_PRIMARY_DOC_EXTENSIONS = ('.htm', '.html', '.xhtml', '.txt')

_SCRIPT_STYLE = re.compile(r'(?is)<(script|style)\b[^>]*>.*?</\1>')
_HTML_TAG = re.compile(r'<[^>]+>')
_INLINE_SPACES = re.compile(r'[ \t\f\v]+')


def html_to_text(raw: str) -> str:
    """Cheap HTML -> text conversion (edgar's rich_to_text costs ~1.3s per 10-K)."""
    text = _SCRIPT_STYLE.sub(' ', raw)
    text = _HTML_TAG.sub(' ', text)
    text = unescape(text).replace('\xa0', ' ')
    return _INLINE_SPACES.sub(' ', text)


def _primary_document_url(filing):
    """URL of the filing's primary document, or None when it is not a text document."""
    primary = (getattr(filing, 'primary_document', '') or '').strip()
    if not primary or os.path.splitext(primary)[1].lower() not in _PRIMARY_DOC_EXTENSIONS:
        return None
    accession = filing.accession_no.replace('-', '')
    return f"{SEC_ARCHIVE_URL}/data/{int(filing.cik)}/{accession}/{primary}"


def _primary_document_text(filing):
    """Download and convert the primary document only (no full SGML package)."""
    url = _primary_document_url(filing)
    if url is None:
        return None
    content = download_file(url, as_text=False)
    if not content:
        return None
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            content = content.decode('latin-1')
    return html_to_text(content)


def get_sections_10k(cik, fyears):
    """Fetch/parse the 10-K Item 1->2 section for several years with ONE Company lookup."""
    company = Company(cik)
    sections = {}
    for fyear in fyears:
        try:
            filing = company.get_filings(form="10-K", year=fyear)[0]
        except Exception:
            sections[fyear] = None
            continue

        try:
            tenk = _primary_document_text(filing)
        except Exception:
            tenk = None

        if not tenk:
            # PDF/paper filings, missing primary doc, unusable encoding, ...
            try:
                tenk = filing.text()
            except Exception:
                sections[fyear] = None
                continue

        extraction = extract_between(tenk)
        if extraction:
            sections[fyear] = extraction
        else:
            sections[fyear] = tenk
    return sections


def get_section_10k(cik:str,fyear:str):
    return get_sections_10k(cik, [fyear]).get(fyear)

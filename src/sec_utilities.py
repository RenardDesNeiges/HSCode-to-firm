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


_NUMBER_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
    'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11,
    'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16,
}
_ITEM_NUMBER = r'(?:\d{1,2}|' + '|'.join(_NUMBER_WORDS) + r')'

# Item headings are frequently split over several HTML lines ("Item\n 1.")
# and older filings spell the number out ("ITEM ONE - BUSINESS").
_ITEM_RE = re.compile(
    r'(?i)(?<![A-Za-z])i\s?t\s?e\s?m(?:s)?\s+'
    rf'(?P<n1>{_ITEM_NUMBER})(?P<l1>[A-Z])?\b'
    rf'(?:\s*[.,]?\s*(?:,|and|&)\s*(?P<n2>{_ITEM_NUMBER})(?P<l2>[A-Z])?\b)?'
)

# Longest first: "business and properties" must win over "business".
_SECTION_TITLES = [
    'business and properties',
    'business and other information',
    'general development of business',
    'general corporate structure and business',
    'general corporate structure',
    'description of business',
    'description of properties',
    'description of property',
    'property and processing facilities',
    'property and processing facility',
    'executive officers of the registrant',
    'executive officers',
    'the business',
    'our business',
    'business',
    'properties',
    'risk factors',
    'unresolved staff comments',
    'legal proceedings',
    'submission of matters to a vote of security holders',
    'mine safety disclosures',
    'mine safety disclosure',
    'market for registrant',
    'market for the registrant',
    'selected financial data',
    "management's discussion and analysis",
    'management s discussion and analysis',
    'management\u2019s discussion and analysis',
    'quantitative and qualitative disclosures',
    'qualitative and quantitative disclosures',
    'financial statements and supplementary data',
    'financial statements',
    'changes in and disagreements',
    'controls and procedures',
    'other information',
    'disclosure regarding foreign jurisdictions',
    'cybersecurity',
    'directors, executive officers',
    'directors and executive officers',
    'executive compensation',
    'security ownership',
    'certain relationships',
    'principal accountant',
    'principal accounting',
    'exhibits',
    'form 10-k summary',
    'reserved',
    '[reserved]',
    '[reserved.]',
]

_TITLE_SEPARATORS = re.compile(r'^[\s\.\:\-\u2013\u2014\|•·,;]*')
_REF_BEFORE = re.compile(
    r'(?i)\b(?:see|under|in|of|part|and|or|to|the|a|an|by|with|as|for|'
    r'from|which|that|on|at|pursuant\s+to|described\s+in|refer\s+to|'
    r'reference\s+to|set\s+forth\s+in|included\s+in|contained\s+in)\s*$'
    r'|\bpart\s+[ivx]+\s*,\s*$'
)
_REF_AFTER = ('"', "'", '\u201d', '\u2019', ',', ';')

# Page footers can sit between a pointer phrase and the item it points at:
#   "(see 5 Table of Contents Item 1A—Risk Factors)"
_REF_BEFORE_WIDE = re.compile(
    r'(?i)\b(?:see|under|described\s+in|refer\s+to|contained\s+in)\b'
    r'[^.;:]{0,90}(?:\btable\s+of\s+contents\b)?\s*$'
)


def _title_pattern(title):
    """Match a title even when HTML inserts spaces inside a word."""
    chars = [re.escape(char) for char in title if not char.isspace()]
    return re.compile(r'\s*'.join(chars), re.IGNORECASE)


# Longest first so "business and properties" wins over "business".
_TITLE_MATCHERS = [(title, _title_pattern(title)) for title in _SECTION_TITLES]
_TOC_TITLE_NUMBER = re.compile(
    r'(?i)(?:business|risk\s+factors|properties|legal\s+proceedings|'
    r'unresolved\s+staff\s+comments|selected\s+financial\s+data|'
    r'management.s\s+discussion|exhibits|signatures|market\s+for)\b'
    r'[^0-9]{0,40}\b\d{1,3}\b'
)


def _normalise_text(text):
    """Collapse all whitespace so headings broken across HTML lines are found."""
    return re.sub(r'[\s\u200b\xa0]+', ' ', text).strip()


def _item_number(token):
    token = token.lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _title_after(text, end):
    """Return (section title, matched text, absolute title offset) or Nones."""
    raw = text[end:end + 260]
    rest = re.sub(_TITLE_SEPARATORS, '', raw)
    base = end + (len(raw) - len(rest))
    match_text = rest.replace('\u2019', "'")
    for title, pattern in _TITLE_MATCHERS:
        match = pattern.match(match_text)
        if match:
            return title, rest[:match.end()], base

    # Multi-registrant filings put the registrant names between the item
    # number and the heading, e.g.
    #   "Item 1 Entergy Corporation ... ENTERGY'S BUSINESS"
    for title, pattern in _TITLE_MATCHERS:
        match = pattern.search(match_text)
        if not match:
            continue
        gap = rest[:match.start()]
        if re.search(r'(?i)\b(?:this report|the report|see|under|pursuant|'
                     r'described|referenced)\b', gap):
            continue
        if len(gap.split()) > 20:
            continue
        return title, rest[match.start():match.end()], base + match.start()
    return None, rest, base


def _is_reference(text, match, title_text, title_start):
    """True when an "Item N" match is an inline cross-reference."""
    before = text[max(0, match.start() - 60):match.start()].rstrip()
    if before.endswith(('\u201c', '\u2018', '(')):
        return True
    if before.endswith('"'):
        # An odd number of double quotes means the heading starts inside a
        # quoted cross-reference; an even number means the quote just closed
        # the previous sentence and this is a real heading.
        window = text[max(0, match.start() - 200):match.start()]
        if window.count('"') % 2 == 1:
            return True
    if _REF_BEFORE.search(before):
        return True
    wide = text[max(0, match.start() - 140):match.start()]
    if _REF_BEFORE_WIDE.search(wide):
        return True
    title_end = title_start + len(title_text)
    tail = text[title_end:title_end + 3].lstrip()
    return bool(tail and tail[0] in _REF_AFTER)


def _looks_like_toc(text, heading, content):
    """Reject table-of-contents entries that look like real headings."""
    if len(content) >= 3000:
        return False
    behind = text[max(0, heading['start'] - 300):heading['start']].lower()
    if 'table of contents' in behind or behind.rstrip().endswith('contents'):
        return True
    if len(_TOC_TITLE_NUMBER.findall(content)) >= 2:
        return True
    return len(re.findall(r'\.{5,}', content)) >= 2


def _find_headings(text):
    headings = []
    for match in _ITEM_RE.finditer(text):
        first = _item_number(match.group('n1'))
        if first is None:
            continue
        numbers = [(first, (match.group('l1') or '').upper())]
        if match.group('n2'):
            second = _item_number(match.group('n2'))
            if second is not None:
                numbers.append((second, (match.group('l2') or '').upper()))
        title, title_text, title_start = _title_after(text, match.end())
        headings.append({
            'start': match.start(),
            'end': match.end(),
            'numbers': numbers,
            'title': title,
            'reference': _is_reference(text, match, title_text, title_start) if title else True,
        })
    return headings


def _allowed_boundary(start_heading, heading):
    first = heading['numbers'][0][0]
    combined = len(start_heading['numbers']) > 1
    if combined:
        # "Items 1 and 2" already contains the Properties section.
        return (first == 1 and heading['numbers'][0][1] != '') or first in (3, 4)
    # A plain Item 1 ends at Item 1A/1B/1C, Item 2, Item 3 or Item 4.
    return (first == 1 and heading['numbers'][0][1] != '') or first in (2, 3, 4)


def _next_boundary(headings, start_heading):
    for heading in headings:
        if heading['start'] <= start_heading['start']:
            continue
        if heading['reference'] or heading['title'] is None:
            continue
        if _allowed_boundary(start_heading, heading):
            return heading
    return None


def extract_business_section(text):
    """Extract the Item 1 "Business" section from a 10-K document.

    Returns the section text, or None when the filing has no usable Item 1
    section.  Heading matching is done on whitespace-normalised text because
    10-K HTML routinely splits "Item 1. Business" across several lines.
    """
    if not text:
        return None
    normalised = _normalise_text(text)
    headings = _find_headings(normalised)

    candidates = []
    for heading in headings:
        if heading['reference'] or heading['title'] is None:
            continue
        if heading['numbers'][0][0] != 1 or 'business' not in heading['title']:
            continue
        boundary = _next_boundary(headings, heading)
        if boundary is None:
            continue
        content = normalised[heading['end']:boundary['start']]
        content = re.sub(_TITLE_SEPARATORS, '', content).strip()
        if not content:
            continue
        candidates.append({
            'heading': heading,
            'content': content,
            'toc': _looks_like_toc(normalised, heading, content),
        })

    if not candidates:
        return None

    # Prefer the earliest substantial, non-TOC section.  This skips the table
    # of contents and stays on the primary registrant when a filing bundles
    # several 10-Ks.
    good = [c for c in candidates if len(c['content']) >= 500 and not c['toc']]
    substantial = [c for c in good if len(c['content']) >= 2000]
    if substantial:
        chosen = min(substantial, key=lambda c: c['heading']['start'])
    elif good:
        chosen = min(good, key=lambda c: c['heading']['start'])
    else:
        chosen = max(candidates, key=lambda c: len(c['content']))

    content = chosen['content']
    return content if len(content) >= 200 else None


def extract_between(text):
    """Backwards-compatible name for the Item 1 Business extractor."""
    return extract_business_section(text)


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


def _filing_period_fyear(filing):
    """Compustat-style fiscal year of a filing's period of report.

    A 52/53-week fiscal year ending in the first days of January belongs to
    the previous calendar year for Compustat (e.g. period 2004-01-03 is
    fiscal year 2003).
    """
    period = getattr(filing, 'period_of_report', None)
    if period is None:
        return None
    if hasattr(period, 'year'):
        year, month, day = period.year, period.month, period.day
    else:
        try:
            year, month, day = int(str(period)[:4]), int(str(period)[5:7]), int(str(period)[8:10])
        except (TypeError, ValueError):
            return None
    return year - 1 if (month == 1 and day <= 7) else year


def _discover_10k_filings(company, fyear):
    """All 10-K variants filed around a fiscal year, for discovery only."""
    fy = int(fyear)
    try:
        filings = list(company.get_filings(year=[fy - 1, fy, fy + 1]))
    except Exception:
        # Older edgar versions do not accept a list of years.
        filings = []
        for year in (fy - 1, fy, fy + 1):
            try:
                filings.extend(company.get_filings(year=year))
            except Exception:
                continue

    filings = [f for f in filings if (f.form or '').upper().startswith('10-K')]

    unique = []
    seen = set()
    for filing in filings:
        accession = getattr(filing, 'accession_no', None) or id(filing)
        if accession in seen:
            continue
        seen.add(accession)
        unique.append(filing)
    return unique


def _filing_date_ordinal(filing):
    filing_date = getattr(filing, 'filing_date', None)
    return filing_date.toordinal() if filing_date else 0


def _matching_filings(filings, fyear):
    """Keep only filings whose period_of_report belongs to ``fyear``.

    The search window in :func:`_discover_10k_filings` is not an acceptance
    window: a 10-K for fiscal year 2000 is often filed in 2001, but its
    period must still be fiscal year 2000.
    """
    fy = int(fyear)
    matching = [f for f in filings if _filing_period_fyear(f) == fy]
    originals = [f for f in matching if '/A' not in (f.form or '').upper()]
    amendments = [f for f in matching if '/A' in (f.form or '').upper()]
    originals.sort(key=_filing_date_ordinal, reverse=True)
    amendments.sort(key=_filing_date_ordinal, reverse=True)
    return originals + amendments


def _candidate_filings(company, fyear):
    """Exact fiscal-year 10-K candidates, best candidate first."""
    return _matching_filings(_discover_10k_filings(company, fyear), fyear)


def get_sections_10k_tagged(cik, fyears):
    """Fetch/parse Item 1 Business sections and a status tag for each year.

    Returns ``{fyear: (section_or_None, tag)}`` where tag is one of
    ``OK``, ``Missing 10-K``, ``No 10-K for fyear``,
    ``No Item 1 Business`` or ``Document error``.

    Only filings whose ``period_of_report`` belongs to ``fyear`` are accepted;
    the surrounding years are used for discovery only.  A missing section is
    never replaced by the full document.
    """
    company = Company(cik)
    sections = {}
    for fyear in fyears:
        discovered = _discover_10k_filings(company, fyear)
        if not discovered:
            sections[fyear] = (None, 'Missing 10-K')
            continue

        filings = _matching_filings(discovered, fyear)
        if not filings:
            sections[fyear] = (None, 'No 10-K for fyear')
            continue

        found = None
        got_text = False
        saw_error = False
        for filing in filings:
            try:
                tenk = _primary_document_text(filing)
            except Exception:
                tenk = None

            if not tenk:
                # PDF/paper filings, missing primary doc, 404, ...
                try:
                    tenk = filing.text()
                except Exception:
                    saw_error = True
                    continue

            if not tenk:
                saw_error = True
                continue

            got_text = True
            extraction = extract_business_section(tenk)
            if extraction:
                found = extraction
                break

        if found:
            sections[fyear] = (found, 'OK')
        elif got_text:
            sections[fyear] = (None, 'No Item 1 Business')
        elif saw_error:
            sections[fyear] = (None, 'Document error')
        else:
            sections[fyear] = (None, 'No Item 1 Business')
    return sections


def get_sections_10k(cik, fyears):
    """Fetch/parse the 10-K Item 1 Business section for several years."""
    tagged = get_sections_10k_tagged(cik, fyears)
    return {fyear: section for fyear, (section, _tag) in tagged.items()}


def get_section_10k(cik: str, fyear: str):
    return get_sections_10k(cik, [fyear]).get(fyear)

#!/usr/bin/env python3
"""Populate the ``filings`` table with HS codes extracted from 10-K filings.

For every row where ``tenk_tag = 'OK'`` and ``hs_tag = 'NO_DATA'`` this script
runs the exact pipeline from ``example_pipeline.py``: parse the 10-K section
into a product list, build a company context, semantically retrieve candidate
HS headings for every product, rerank them with the decision model, and let the
model remove irrelevant first-guess codes.

Each filing is processed in a worker thread, but only the main thread touches
SQLite. Every completed row is committed immediately, so an interrupted run
can simply be restarted and will pick up the remaining rows.

Usage
-----
    python parse_tenk.py --workers 8
    python parse_tenk.py --limit 5
    python parse_tenk.py --retry-errors

Notes
-----
* ``src.sec_product_scrapper`` is intentionally **not** imported: that module
  instantiates ``SemanticSearch()`` (loading the embedding model) at import
  time. We load it exactly once in ``main`` instead.
* ``get_first_guess_list`` and the search-terms prompt are reimplemented here
  with the Excel file load hoisted out of the per-product loop.
* The progress bar shows the cumulative average ``elapsed / completed`` (tqdm
  smoothing is disabled), and an optional multi-line panel below it lists the
  firm-years currently running plus the next ones waiting in the queue.
* Non-fatal per-filing errors are written to the log file only; the terminal
  shows just the progress bar/panel and the final summary. Third-party startup
  output from the Hugging Face Hub, transformers and torch is suppressed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import random
import shutil
import signal
import sqlite3
import sys
import textwrap
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

# Keep third-party startup chatter (HF Hub warnings, weight-loading bars,
# transformers load reports, torch import notices) out of the terminal.
# These must be set before sentence-transformers / transformers / torch are
# imported, which happens through ``src.semantic_search`` below.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

for _noisy_logger in (
    "huggingface_hub",
    "huggingface_hub.utils._http",
    "transformers",
    "sentence_transformers",
    "torch.distributed.elastic.multiprocessing.redirects",
):
    logging.getLogger(_noisy_logger).setLevel(logging.ERROR)

import numpy as np
import pandas as pd
from tqdm import tqdm

from src import llm_utilities
from src.semantic_search import SemanticSearch


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = ROOT_DIR / "data" / "hsmap.db"
DEFAULT_ERROR_LOG = ROOT_DIR / "data" / "parse_tenk_errors.log"
HSCODES_PATH = ROOT_DIR / "data" / "HSCodeandDescription.xlsx"

MAX_BACKOFF_SECONDS = 60.0
TRANSIENT_MARKERS = (
    "TooManyRequests",
    "RequestTimeout",
    "ProviderOverloaded",
    "ServiceUnavailable",
    "BadGateway",
    "EdgeNetworkTimeout",
    "InternalServer",
    "NoResponse",
    "TransportError",
    "Timeout",
    "ConnectError",
    "Connection",
    "ResponseValidation",
)

logger = logging.getLogger("parse_tenk")

# Populated once in main(), before any worker thread starts.
_SEMSEARCH: SemanticSearch | None = None
_HS4: pd.DataFrame | None = None
_HS_CHAPTERS: str = ""
_SEM_LOCK = threading.Lock()

# Lazily cached search-terms prompt template.
_PROMPT_TEMPLATE: str | None = None
_PROMPT_LOCK = threading.Lock()


@dataclass
class FilingResult:
    """Outcome of processing one ``filings`` row."""

    key: int
    cik: str | None
    conm: str | None
    fyear: str | None
    hs_codes: str
    hs_output: str
    hs_tag: str
    duration_s: float


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run the 10-K -> product -> HS-code pipeline for pending filings "
            "and write hs_codes / hs_output / hs_tag back to data/hsmap.db."
        )
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="Path to the SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of filing rows processed in parallel (default: %(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N pending rows (default: all)",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Also retry rows whose hs_tag is ERROR:* (default: off)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retries for transient LLM/HTTP errors (default: %(default)s)",
    )
    parser.add_argument(
        "--error-log",
        default=str(DEFAULT_ERROR_LOG),
        help="Log file for errors and per-row progress (default: %(default)s)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable the tqdm progress bar and the queue panel",
    )
    parser.add_argument(
        "--queue-lines",
        type=int,
        default=None,
        help=(
            "Firm-year lines shown below the progress bar (default: auto "
            "from --workers and terminal height; 0 hides the panel)"
        ),
    )

    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.max_retries < 0:
        parser.error("--max-retries must be >= 0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.queue_lines is not None and args.queue_lines < 0:
        parser.error("--queue-lines must be >= 0")
    return args


def _configure_logging(log_path):
    """Send all diagnostics to ``log_path`` only.

    The terminal is reserved for the progress bar, panel and final summary.
    Per-filing failures are non-fatal: their tracebacks go to the log file,
    while the counters on the progress bar and in the summary show that they
    happened.
    """
    log_path = os.path.abspath(os.path.expanduser(str(log_path)))
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return log_path


def _install_signal_handlers():
    """Make SIGTERM behave like Ctrl+C so shutdown logic runs."""

    def _raise_keyboard_interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        # signal() only works in the main thread; ignore elsewhere.
        pass


def _connect(path):
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _verify_schema(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(filings)")}
    if not columns:
        raise SystemExit("ERROR: table 'filings' was not found in the database")

    required = {
        "key",
        "cik",
        "conm",
        "fyear",
        "tenk_section",
        "tenk_tag",
        "hs_codes",
        "hs_output",
        "hs_tag",
    }
    missing = sorted(required - columns)
    if missing:
        raise SystemExit(
            "ERROR: table 'filings' is missing required column(s): "
            + ", ".join(missing)
        )


def _pending_clause(retry_errors):
    if retry_errors:
        return "(hs_tag = 'NO_DATA' OR hs_tag LIKE 'ERROR:%')"
    return "hs_tag = 'NO_DATA'"


def _pending_sql(retry_errors, limit, *, count):
    selection = "COUNT(*)" if count else "key, cik, conm, fyear, tenk_section"
    sql = (
        "SELECT " + selection + " FROM filings "
        "WHERE tenk_tag = 'OK' "
        "AND tenk_section IS NOT NULL "
        "AND tenk_section <> '' "
        "AND " + _pending_clause(retry_errors)
    )
    if not count:
        sql += " ORDER BY key"
    if limit is not None:
        sql += " LIMIT ?"
    return sql


def _count_pending(conn, retry_errors, limit=None):
    if limit is None:
        sql = _pending_sql(retry_errors, None, count=True)
        row = conn.execute(sql).fetchone()
    else:
        inner = _pending_sql(retry_errors, limit, count=False)
        row = conn.execute(
            "SELECT COUNT(*) FROM (" + inner + ")", (limit,)
        ).fetchone()
    return int(row[0]) if row else 0


def _fetch_pending(conn, retry_errors, limit=None):
    sql = _pending_sql(retry_errors, limit, count=False)
    params = () if limit is None else (limit,)
    return conn.execute(sql, params).fetchall()


def _is_transient(exc):
    """Return True only for errors that are plausibly worth retrying."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    for cls in type(exc).__mro__:
        name = cls.__name__
        if any(marker in name for marker in TRANSIENT_MARKERS):
            return True
    return False


def _call_with_retries(func, *args, max_retries=3, **kwargs):
    """Call ``func`` with exponential backoff + jitter on transient errors."""
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt >= max_retries or not _is_transient(exc):
                raise
            base_delay = min(MAX_BACKOFF_SECONDS, 2.0 * (2 ** attempt))
            delay = min(MAX_BACKOFF_SECONDS, base_delay * (1.0 + random.random()))
            logger.warning(
                "Transient %s in %s (attempt %d/%d); retrying in %.1fs: %s",
                type(exc).__name__,
                getattr(func, "__name__", str(func)),
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            time.sleep(delay)
            attempt += 1


def _to_jsonable(obj):
    """Recursively convert ``obj`` into something ``json.dumps`` accepts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _to_jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(key): _to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (set, frozenset)):
        converted = [_to_jsonable(value) for value in obj]
        return sorted(converted, key=str)
    if isinstance(obj, (tuple, list)):
        return [_to_jsonable(value) for value in obj]
    if isinstance(obj, pd.DataFrame):
        return _to_jsonable(obj.to_dict(orient="records"))
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _to_jsonable(obj.tolist())
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _first_guess_list(product_data):
    """Build the ``CODE : description`` list, in Excel row order.

    Mirrors ``get_first_guess_list`` / ``parse_tick_data`` from
    ``src/sec_product_scrapper.py`` without importing that module.
    """
    codes = set()
    for item in product_data.values():
        result = item.get("result")
        if result is None:
            continue
        for code in result.codes:
            code_str = str(code)
            if code_str.isdigit():
                codes.add(code_str)

    if not codes or _HS4 is None:
        return ""

    rows = _HS4[_HS4["Code"].isin(codes)]
    return "".join(
        f"{row['Code']} : {row['Description']}\n" for _, row in rows.iterrows()
    )


def _get_search_terms_template():
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        with _PROMPT_LOCK:
            if _PROMPT_TEMPLATE is None:
                prompt_path = os.path.join(
                    llm_utilities.PROMPT_DIR, "generate_search_terms.txt"
                )
                with open(prompt_path, "r", encoding="utf-8") as handle:
                    _PROMPT_TEMPLATE = handle.read()
    return _PROMPT_TEMPLATE


def cached_generate_search_terms(product, max_retries=3):
    """Equivalent to ``llm_utilities.generate_search_terms``.

    The only difference is that the prompt template and the HS-chapter list
    are cached/hoisted out of the per-product call, so we do not re-read the
    Excel workbook for every product. ``src.sec_product_scrapper`` is not
    imported because it loads the embedding model at import time.
    """
    template = _get_search_terms_template()
    prompt = template.replace("{query}", product).replace(
        "{hs_chapters}", _HS_CHAPTERS
    )
    return _call_with_retries(
        llm_utilities.query_a_model_openai,
        prompt,
        llm_utilities.CHEAP_MODEL,
        max_retries=max_retries,
    )


def _error_payload(stage, exc):
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


def _run_pipeline(conm, tenk_section, max_retries):
    """Run the example pipeline; never raises for expected pipeline errors.

    Returns ``(company, productlist, first_error)``. Filing-level failures are
    stored in ``company['error']``; product-level failures are stored in the
    corresponding product item and do not discard successful products.
    """
    company = {}
    productlist = []
    product_data = {}
    first_error = None

    def record(stage, exc, detail=""):
        nonlocal first_error
        if first_error is None:
            first_error = exc
        logger.error(
            "Pipeline stage %s failed%s: %s",
            stage,
            detail,
            exc,
            exc_info=True,
        )

    try:
        tenk = llm_utilities.secure_sec_length(tenk_section)
    except Exception as exc:
        record("secure_sec_length", exc)
        company["error"] = _error_payload("secure_sec_length", exc)
        return company, productlist, first_error

    try:
        products = _call_with_retries(
            llm_utilities.parse_filing,
            tenk,
            max_retries=max_retries,
        )
        if not isinstance(products, str):
            raise TypeError(
                "parse_filing returned "
                + type(products).__name__
                + " instead of str"
            )
    except Exception as exc:
        record("parse_filing", exc)
        company["error"] = _error_payload("parse_filing", exc)
        return company, productlist, first_error

    try:
        context = _call_with_retries(
            llm_utilities.parse_filing_context,
            conm,
            max_retries=max_retries,
        )
    except Exception as exc:
        record("parse_filing_context", exc)
        company["error"] = _error_payload("parse_filing_context", exc)
        return company, productlist, first_error

    company["context"] = context
    productlist = [part.strip() for part in products.split(",") if part.strip()]

    for product in productlist:
        item = {"product": product}
        try:
            item["search_terms"] = cached_generate_search_terms(
                product, max_retries=max_retries
            )
            with _SEM_LOCK:
                candidates = _SEMSEARCH.get_candidates(
                    product + "\n" + item["search_terms"],
                    candidate_count=20,
                )
            item["candidates"] = candidates
            item["result"] = _call_with_retries(
                llm_utilities.parse_product_hs,
                candidates.to_string(index=False),
                product,
                context,
                max_retries=max_retries,
            )
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            record(
                "product",
                exc,
                f" (product={product!r}, conm={conm!r})",
            )
        product_data[product] = item

    company["product data"] = product_data

    ig_codes = ""
    try:
        ig_codes = _first_guess_list(product_data)
    except Exception as exc:
        record("first_guess_list", exc, f" (conm={conm!r})")
        company["error"] = _error_payload("first_guess_list", exc)
    company["ig_codes"] = ig_codes

    removed = set()
    if ig_codes:
        try:
            removed = _call_with_retries(
                llm_utilities.refine_product_list,
                ig_codes,
                context,
                max_retries=max_retries,
            )
            removed = set(removed or ())
        except Exception as exc:
            record("refine_product_list", exc, f" (conm={conm!r})")
            company["error"] = _error_payload("refine_product_list", exc)
            removed = set()
    company["removed"] = removed

    code_probabilities = {}
    for item in product_data.values():
        result = item.get("result")
        if result is None:
            continue
        for code in result.codes:
            code_probabilities[code] = max(
                code_probabilities.get(code, 0.0),
                result.probabilities.get(code, 0.0),
            )
    company["hs_probabilities"] = [
        (code, probability)
        for code, probability in sorted(code_probabilities.items())
        if code not in removed
    ]

    codes = set()
    for item in product_data.values():
        result = item.get("result")
        if result is not None:
            codes.update(result.codes)
    company["codes"] = codes - removed

    return company, productlist, first_error


def process_filing(row, max_retries=3):
    """Process one filing row in a worker thread. Never raises."""
    start = time.monotonic()
    key = cik = conm = fyear = None
    tenk_section = ""

    try:
        key, cik, conm, fyear, tenk_section = row
        company, productlist, first_error = _run_pipeline(
            conm or "",
            tenk_section or "",
            max_retries,
        )
        codes = company.get("codes") or set()
        hs_codes = ",".join(sorted(str(code) for code in codes))

        if first_error is not None:
            hs_tag = "ERROR:" + type(first_error).__name__
        elif not productlist or not codes:
            hs_tag = "EMPTY"
        else:
            hs_tag = "OK"

        hs_output = json.dumps(_to_jsonable(company), ensure_ascii=False)
    except Exception as exc:
        logger.error(
            "Catastrophic failure for key=%s cik=%s conm=%r: %s",
            key,
            cik,
            conm,
            exc,
            exc_info=True,
        )
        company = {"error": _error_payload("process_filing", exc)}
        hs_codes = ""
        hs_tag = "ERROR:" + type(exc).__name__
        try:
            hs_output = json.dumps(_to_jsonable(company), ensure_ascii=False)
        except Exception:
            hs_output = "{}"

    duration_s = time.monotonic() - start
    return FilingResult(
        key=key,
        cik=cik,
        conm=conm,
        fyear=fyear,
        hs_codes=hs_codes,
        hs_output=hs_output,
        hs_tag=hs_tag,
        duration_s=duration_s,
    )


def _write_result(conn, result):
    conn.execute(
        "UPDATE filings SET hs_codes = ?, hs_output = ?, hs_tag = ? "
        "WHERE key = ?",
        (result.hs_codes, result.hs_output, result.hs_tag, result.key),
    )
    conn.commit()
    logger.info(
        "key=%s cik=%s conm=%r fyear=%s tag=%s duration=%.1fs",
        result.key,
        result.cik,
        result.conm,
        result.fyear,
        result.hs_tag,
        result.duration_s,
    )


def _result_bucket(tag):
    if tag == "OK":
        return "ok"
    if tag == "EMPTY":
        return "empty"
    return "error"


def _format_elapsed(seconds):
    """Format a duration as ``MM:SS`` or ``H:MM:SS``."""
    seconds = int(max(0.0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _is_interactive():
    """Return True when stderr is an interactive terminal."""
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


def _auto_queue_lines(workers):
    """Choose a sensible number of queue lines for the current terminal."""
    try:
        terminal_lines = shutil.get_terminal_size().lines
    except OSError:
        terminal_lines = 24
    # Leave room for the main bar and the final summary.
    return max(1, min(workers + 2, max(1, terminal_lines - 3)))


def _queue_line(row, status, elapsed, width):
    """Render one firm-year line for the queue panel."""
    _key, cik, conm, fyear, _section = row
    cik = str(cik or "")
    fyear = str(fyear or "")
    conm = " ".join(str(conm or "").split())

    if status == "run":
        prefix = f"[run  {_format_elapsed(elapsed):>8}] "
    else:
        prefix = "[next        ] "

    suffix = f" {fyear:>4}"
    available = max(8, width - len(prefix) - len(cik) - len(suffix) - 2)
    conm = textwrap.shorten(conm, width=available, placeholder="…")
    return f"{prefix}{cik:<10} {conm:<{available}}{suffix}"


class _QueuePanel:
    """Live multi-line list of firm-years drawn below the progress bar.

    The first lines show the filings that are currently running; any remaining
    lines show the next filings waiting in the queue.  The panel is only drawn
    when stderr is an interactive terminal, so redirected logs stay clean.
    """

    def __init__(self, line_count, enabled=True):
        self.line_count = max(0, int(line_count))
        self.enabled = bool(enabled and self.line_count > 0)
        self.bars = []
        if not self.enabled:
            return
        try:
            for index in range(self.line_count):
                self.bars.append(
                    tqdm(
                        total=0,
                        position=index + 1,
                        bar_format="{desc}",
                        desc="",
                        leave=False,
                        dynamic_ncols=True,
                        disable=False,
                    )
                )
        except Exception:
            logger.debug("Could not create the queue panel", exc_info=True)
            self.bars = []
            self.enabled = False

    def update(self, running, waiting):
        if not self.enabled:
            return
        try:
            width = shutil.get_terminal_size().columns
            lines = [
                _queue_line(row, "run", time.monotonic() - started, width)
                for row, started in running[: self.line_count]
            ]
            if len(lines) < self.line_count:
                for row in waiting[: self.line_count - len(lines)]:
                    lines.append(_queue_line(row, "next", 0.0, width))
            for bar, line in zip(self.bars, lines):
                bar.set_description_str(line, refresh=False)
            for bar in self.bars:
                bar.refresh()
        except Exception:
            # A display problem must never stop the actual processing.
            logger.debug("Queue panel update failed; disabling it", exc_info=True)
            self.close()

    def close(self):
        for bar in self.bars:
            try:
                bar.close()
            except Exception:
                pass
        self.bars = []
        self.enabled = False


def _commit_finished(conn, futures, written, stats):
    """Commit results of futures that finished while the loop was stopping."""
    for future in futures:
        if future.cancelled() or not future.done():
            continue
        try:
            result = future.result()
        except Exception as exc:
            logger.error(
                "Worker future failed during shutdown: %s", exc, exc_info=True
            )
            continue
        if result.key in written:
            continue
        try:
            _write_result(conn, result)
        except Exception as exc:
            logger.error(
                "Failed to write key=%s during shutdown: %s",
                result.key,
                exc,
                exc_info=True,
            )
            continue
        written.add(result.key)
        stats[_result_bucket(result.hs_tag)] += 1


def _print_summary(stats, elapsed_s, log_path):
    print(
        "Processed: {processed} | OK: {ok} | EMPTY: {empty} | ERROR: {error} "
        "| elapsed: {elapsed:.1f}s".format(
            processed=stats["ok"] + stats["empty"] + stats["error"],
            ok=stats["ok"],
            empty=stats["empty"],
            error=stats["error"],
            elapsed=elapsed_s,
        )
    )
    print(f"Error log: {log_path}")


def main(argv=None):
    args = _parse_args(argv)
    log_path = _configure_logging(args.error_log)
    _install_signal_handlers()

    api_key = str(getattr(llm_utilities, "API_KEY", "") or "")
    if not api_key.strip() or "your-openrouter-api-key-here" in api_key.lower():
        print(
            "ERROR: llm_utilities.API_KEY is not set or still contains the "
            "example_config.py placeholder. Configure config.py first.",
            file=sys.stderr,
        )
        return 2

    db_path = os.path.abspath(os.path.expanduser(str(args.db)))
    if not os.path.isfile(db_path):
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 2

    conn = _connect(db_path)
    try:
        _verify_schema(conn)

        pending_count = _count_pending(conn, args.retry_errors, args.limit)
        print(f"Database: {db_path}")
        print(f"Pending filings: {pending_count}")
        print(f"Workers: {args.workers}")

        if pending_count == 0:
            print("Nothing to do")
            return 0

        rows = _fetch_pending(conn, args.retry_errors, args.limit)

        global _SEMSEARCH, _HS4, _HS_CHAPTERS
        semsearch = SemanticSearch(product_codes=str(HSCODES_PATH))
        _SEMSEARCH = semsearch
        _HS4 = semsearch.hscodes4digits[["Code", "Description"]].copy()
        _HS_CHAPTERS = semsearch.hscodes[
            semsearch.hscodes["Level"] == 2
        ]["Description"].to_string(index=False, header=False)

        stats = {"ok": 0, "empty": 0, "error": 0}
        written = set()
        start = time.monotonic()

        queue_lines = args.queue_lines
        if queue_lines is None:
            queue_lines = _auto_queue_lines(args.workers)
        panel_enabled = (
            not args.quiet
            and queue_lines > 0
            and _is_interactive()
        )

        executor = ThreadPoolExecutor(max_workers=args.workers)
        pbar = tqdm(
            total=len(rows),
            desc="Parsing 10-K filings",
            unit="filing",
            smoothing=0,  # cumulative elapsed/completed, not an EMA
            position=0,
            dynamic_ncols=True,
            disable=args.quiet,
            bar_format=(
                "{l_bar}{bar}| {n_fmt}/{total_fmt} "
                "[{elapsed}<{remaining}, {rate_inv_fmt}{postfix}]"
            ),
        )
        panel = _QueuePanel(queue_lines, enabled=panel_enabled)

        # Rolling window: keep at most --workers tasks in flight so the panel
        # can report exactly which filings are running and which are next.
        in_flight = {}
        next_index = 0

        def submit_next():
            nonlocal next_index
            if next_index >= len(rows):
                return False
            row = rows[next_index]
            next_index += 1
            future = executor.submit(process_filing, row, args.max_retries)
            in_flight[future] = (row, time.monotonic())
            return True

        for _ in range(args.workers):
            if not submit_next():
                break
        pbar.set_postfix(
            ok=0,
            empty=0,
            error=0,
            waiting=len(rows) - next_index,
        )

        try:
            while in_flight:
                done, _ = wait(
                    tuple(in_flight),
                    timeout=1.0,
                    return_when=FIRST_COMPLETED,
                )

                if panel.enabled:
                    panel.update(
                        list(in_flight.values()),
                        rows[next_index: next_index + queue_lines],
                    )

                for future in done:
                    row, _submitted_at = in_flight.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        key, cik, conm, fyear, _tenk = row
                        logger.error(
                            "Worker raised unexpectedly for key=%s cik=%s: %s",
                            key,
                            cik,
                            exc,
                            exc_info=True,
                        )
                        result = FilingResult(
                            key=key,
                            cik=cik,
                            conm=conm,
                            fyear=fyear,
                            hs_codes="",
                            hs_output=json.dumps(
                                {"error": _error_payload("worker", exc)},
                                ensure_ascii=False,
                            ),
                            hs_tag="ERROR:" + type(exc).__name__,
                            duration_s=0.0,
                        )

                    try:
                        _write_result(conn, result)
                    except Exception as exc:
                        logger.error(
                            "Failed to write result for key=%s: %s",
                            result.key,
                            exc,
                            exc_info=True,
                        )
                    else:
                        written.add(result.key)
                        stats[_result_bucket(result.hs_tag)] += 1

                    pbar.update(1)
                    submit_next()

                if done:
                    pbar.set_postfix(
                        ok=stats["ok"],
                        empty=stats["empty"],
                        error=stats["error"],
                        waiting=len(rows) - next_index,
                    )
        except KeyboardInterrupt:
            # Ensure we always terminate, even if summary printing fails:
            # a normal return would make ThreadPoolExecutor's atexit hook
            # join in-flight LLM requests and hang the process.
            try:
                panel.close()
                pbar.close()
                print(
                    "\nInterrupted; committing already-finished rows ...",
                    file=sys.stderr,
                )
                executor.shutdown(wait=False, cancel_futures=True)
                _commit_finished(conn, list(in_flight), written, stats)
                _print_summary(stats, time.monotonic() - start, log_path)
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(130)
        finally:
            panel.close()
            pbar.close()
            executor.shutdown(wait=True)

        elapsed = time.monotonic() - start
        _print_summary(stats, elapsed, log_path)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Parse pending 10-K filings into HS codes and write them back to hsmap.db."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

for _name in (
    "huggingface_hub",
    "huggingface_hub.utils._http",
    "transformers",
    "sentence_transformers",
    "torch.distributed.elastic.multiprocessing.redirects",
):
    logging.getLogger(_name).setLevel(logging.ERROR)

from src import llm_utilities
from src.parallel_runner import run_parallel
from src.semantic_search import SemanticSearch

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = str(ROOT_DIR / "data" / "hsmap.db")
ERROR_LOG = str(ROOT_DIR / "data" / "parse_tenk_errors.log")
HSCODES_PATH = str(ROOT_DIR / "data" / "HSCodeandDescription.xlsx")

MAX_RETRIES = 3
MAX_BACKOFF = 60.0
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
_SEMSEARCH = None
_HS4 = None
_HS_CHAPTERS = ""
_SEM_LOCK = threading.Lock()
_PROMPT_TEMPLATE = None
_PROMPT_LOCK = threading.Lock()


@dataclass
class FilingResult:
    key: int
    cik: str
    conm: str
    fyear: str
    hs_codes: str
    hs_output: str
    hs_tag: str
    duration_s: float


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--queue-lines", type=int, default=None)
    return parser.parse_args()


def _configure_logging():
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(ERROR_LOG, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    )
    logger.addHandler(handler)


def _connect(path):
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _fetch_pending(conn):
    return conn.execute(
        "SELECT key, cik, conm, fyear, tenk_section FROM filings "
        "WHERE tenk_tag = 'OK' "
        "AND tenk_section IS NOT NULL "
        "AND tenk_section <> '' "
        "AND (hs_tag = 'NO_DATA' OR hs_tag LIKE 'ERROR:%') "
        "ORDER BY key"
    ).fetchall()


def _is_transient(exc):
    return isinstance(exc, (TimeoutError, ConnectionError)) or any(
        marker in type(exc).__name__ for marker in TRANSIENT_MARKERS
    )


def _call_with_retries(func, *args):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return func(*args)
        except Exception as exc:
            if attempt == MAX_RETRIES or not _is_transient(exc):
                raise
            delay = min(MAX_BACKOFF, 2.0 * 2**attempt) * (1 + random.random())
            logger.warning(
                "%s in %s; retrying in %.1fs: %s",
                type(exc).__name__,
                func.__name__,
                delay,
                exc,
            )
            time.sleep(delay)


def _first_guess_list(product_data):
    codes = set()
    for item in product_data.values():
        result = item.get("result")
        if result is not None:
            codes.update(code for code in result.codes if code.isdigit())
    if not codes:
        return ""
    rows = _HS4[_HS4["Code"].isin(codes)]
    return "".join(
        f"{row['Code']} : {row['Description']}\n" for _, row in rows.iterrows()
    )


def _search_terms(product):
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        with _PROMPT_LOCK:
            if _PROMPT_TEMPLATE is None:
                path = os.path.join(
                    llm_utilities.PROMPT_DIR, "generate_search_terms.txt"
                )
                with open(path, "r", encoding="utf-8") as handle:
                    _PROMPT_TEMPLATE = handle.read()
    prompt = _PROMPT_TEMPLATE.replace("{query}", product).replace(
        "{hs_chapters}", _HS_CHAPTERS
    )
    return _call_with_retries(
        llm_utilities.query_a_model_openai, prompt, llm_utilities.CHEAP_MODEL
    )


def _error_payload(stage, exc):
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


def _run_pipeline(conm, tenk_section):
    company = {}
    product_data = {}
    productlist = []
    first_error = None

    def record(stage, exc, detail=""):
        nonlocal first_error
        if first_error is None:
            first_error = exc
        logger.error("Stage %s failed%s: %s", stage, detail, exc, exc_info=True)

    try:
        tenk = llm_utilities.secure_sec_length(tenk_section)
    except Exception as exc:
        record("secure_sec_length", exc)
        company["error"] = _error_payload("secure_sec_length", exc)
        return company, productlist, first_error

    try:
        products = _call_with_retries(llm_utilities.parse_filing, tenk)
        if not isinstance(products, str):
            raise TypeError(f"parse_filing returned {type(products).__name__}")
    except Exception as exc:
        record("parse_filing", exc)
        company["error"] = _error_payload("parse_filing", exc)
        return company, productlist, first_error

    try:
        context = _call_with_retries(llm_utilities.parse_filing_context, conm)
    except Exception as exc:
        record("parse_filing_context", exc)
        company["error"] = _error_payload("parse_filing_context", exc)
        return company, productlist, first_error

    company["context"] = context
    productlist = [part.strip() for part in products.split(",") if part.strip()]

    for product in productlist:
        item = {"product": product}
        try:
            item["search_terms"] = _search_terms(product)
            with _SEM_LOCK:
                candidates = _SEMSEARCH.get_candidates(
                    product + "\n" + item["search_terms"], candidate_count=20
                )
            item["candidates"] = candidates
            item["result"] = _call_with_retries(
                llm_utilities.parse_product_hs,
                candidates.to_string(index=False),
                product,
                context,
            )
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            record("product", exc, f" (product={product!r})")
        product_data[product] = item

    company["product data"] = product_data

    try:
        ig_codes = _first_guess_list(product_data)
    except Exception as exc:
        record("first_guess_list", exc)
        company["error"] = _error_payload("first_guess_list", exc)
        ig_codes = ""
    company["ig_codes"] = ig_codes

    removed = set()
    if ig_codes:
        try:
            removed = set(
                _call_with_retries(
                    llm_utilities.refine_product_list, ig_codes, context
                )
                or ()
            )
        except Exception as exc:
            record("refine_product_list", exc)
            company["error"] = _error_payload("refine_product_list", exc)

    probabilities = {}
    codes = set()
    for item in product_data.values():
        result = item.get("result")
        if result is None:
            continue
        codes.update(result.codes)
        for code in result.codes:
            probabilities[code] = max(
                probabilities.get(code, 0.0),
                result.probabilities.get(code, 0.0),
            )

    company["removed"] = sorted(removed)
    company["hs_probabilities"] = sorted(
        (code, probability)
        for code, probability in probabilities.items()
        if code not in removed
    )
    company["codes"] = sorted(codes - removed)

    for item in product_data.values():
        if "candidates" in item:
            item["candidates"] = item["candidates"].to_dict(orient="records")
        if "result" in item:
            item["result"] = asdict(item["result"])

    return company, productlist, first_error


def process_filing(row):
    start = time.monotonic()
    key = cik = conm = fyear = None
    try:
        key, cik, conm, fyear, tenk_section = row
        company, productlist, first_error = _run_pipeline(
            conm or "", tenk_section or ""
        )
        codes = company.get("codes") or []
        hs_codes = ",".join(codes)
        if first_error is not None:
            hs_tag = "ERROR:" + type(first_error).__name__
        elif not productlist or not codes:
            hs_tag = "EMPTY"
        else:
            hs_tag = "OK"
        hs_output = json.dumps(company, ensure_ascii=False)
    except Exception as exc:
        logger.error(
            "Filing failed for key=%s cik=%s: %s", key, cik, exc, exc_info=True
        )
        company = {"error": _error_payload("process_filing", exc)}
        hs_codes = ""
        hs_tag = "ERROR:" + type(exc).__name__
        hs_output = json.dumps(company, ensure_ascii=False)
    return FilingResult(
        key=key,
        cik=cik,
        conm=conm,
        fyear=fyear,
        hs_codes=hs_codes,
        hs_output=hs_output,
        hs_tag=hs_tag,
        duration_s=time.monotonic() - start,
    )


def _write_result(conn, result):
    conn.execute(
        "UPDATE filings SET hs_codes = ?, hs_output = ?, hs_tag = ? WHERE key = ?",
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
    return "ok" if tag == "OK" else "empty" if tag == "EMPTY" else "error"


def _print_summary(stats, elapsed_s):
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
    print(f"Error log: {ERROR_LOG}")


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    args = _parse_args()
    _configure_logging()
    signal.signal(signal.SIGTERM, _interrupt)
    conn = _connect(args.db)
    try:
        rows = _fetch_pending(conn)
        print(f"Database: {args.db}")
        print(f"Pending filings: {len(rows)}")
        print(f"Workers: {args.workers}")
        if not rows:
            print("Nothing to do")
            return 0

        global _SEMSEARCH, _HS4, _HS_CHAPTERS
        semsearch = SemanticSearch(product_codes=HSCODES_PATH)
        _SEMSEARCH = semsearch
        _HS4 = semsearch.hscodes4digits[["Code", "Description"]].copy()
        _HS_CHAPTERS = semsearch.hscodes[
            semsearch.hscodes["Level"] == 2
        ]["Description"].to_string(index=False, header=False)

        def store(row, result):
            try:
                _write_result(conn, result)
            except Exception as exc:
                logger.error(
                    "Failed to write result for key=%s: %s",
                    result.key,
                    exc,
                    exc_info=True,
                )
                return None
            return _result_bucket(result.hs_tag)

        def handle_error(row, exc):
            key, cik, conm, fyear, _ = row
            logger.error(
                "Worker failed for key=%s cik=%s: %s",
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
            return store(row, result)

        stats = {"ok": 0, "empty": 0, "error": 0}
        start = time.monotonic()
        try:
            run_parallel(
                rows,
                process_filing,
                store,
                handle_error,
                stats,
                workers=args.workers,
                queue_lines=args.queue_lines,
                desc="Parsing 10-K filings",
            )
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            _print_summary(stats, time.monotonic() - start)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(130)

        _print_summary(stats, time.monotonic() - start)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

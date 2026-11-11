# Reproduction Repo - Product-to-Firm Mapping from SEC 10-K Filings

## Todolist 
- Simplify the pipeline uses CIK keys and nothing else (no tickers)
- Update the pipeline so it queries firm/years instead of just firms
- Update the pipeline so it relies on a SQLite DB instead of text files



| key |  cik |  fyear | 10-k section | hs-codes |
|---|---|---|---|---|
| int | text | text | text (quite long) | list of text elements, unknown lenght | 

This repository holds the replication files that build a product-to-firm mapping from SEC 10-K filings. The pipeline reads a list of CIKs, downloads each firm's latest 10-K filing, extracts the physical product portfolio, and maps each product to 4-digit HS codes. The resulting mapping links HS codes to firms and can be used with Compustat or other firm-level panels.
This pipeline is meant to be ran to replicate the results of the [regressions of my master thesis](https://github.com/RenardDesNeiges/USChina_thesis_reproduction).

```mermaid
flowchart TD
    CIK["CIK"] -->|retrieval from the SEC EDGAR DB| 10K["10-K Filing (in md format)"]
    10K -->|extraction using qwen2.5:7B| PL["Natural language product List"]
    10K -->|extraction using qwen2.5:7B| CTX["Company context"]
    PL -->|generation using qwen2.5:7B| ST["HS search terms"]
    ST -->|extraction using semantic similarity with all-MiniLM-L6-v2| CDD["Product candidates HS Codes"]
    PL -->|extraction using semantic similarity with all-MiniLM-L6-v2| CDD["Product candidates HS Codes"]
    CDD -->|reranking using Kev-4B (decision model)| RR["First guess of product HS Codes"]
    CTX -->|reranking using Kev-4B (decision model)| RR
    RR -->|Pruning of gueses with deepseek-v4-flash| FL["Final HS codes"]

```

## Repository layout

|File/root folder | Use |
|---|---|
|`scrap_sec_data.py` | Entry point. Runs the full pipeline. |
|`src/` | Python modules for SEC download, LLM calls, and semantic search. |
|`prompts/` | LLM prompt templates. |
|`data/` | Stores the CIK list, the HS code reference, and generated mappings. |

Use `example_config.py` as the template for your own local config file.

## Requirements

- Python 3.13 with the following packages: `edgar`, `openrouter`, `tiktoken`, `deepseek_tokenizer`, `sentence-transformers`, `pandas`, `numpy`, `tqdm`.
- An [OpenRouter](https://openrouter.ai/) API key. The same key is used for every LLM step: product extraction and search-term generation call Qwen, pruning calls DeepSeek, and the HS-code reranking step calls `jaredpalmer/kev-4b` through OpenRouter's Decisions API.
- A valid SEC identity (name and email) for EDGAR downloads.

## Configuration

1. Copy `example_config.py` to `config.py` in the repository root.
2. Add your OpenRouter API key.
3. Add your SEC name and email.

Git ignores `config.py` because it holds secrets. Do not commit it.

## Reproduce the results

Run the full pipeline:

```bash
python scrap_sec_data.py
```

Default inputs and outputs:

| Flag | Default | Description |
|---|---|---|
| `-t` | `data/ciks.txt` | Comma-separated list of CIKs. |
| `-o` | `data/product_data/run.txt` | Output mapping file. |
| `-e` | `data/product_data/run_error_log.txt` | Error log file. |
| `-p` | `data/product_data/data_log.txt` | Pickle dump of the full pipeline object. |

Run with custom paths:

```bash
python scrap_sec_data.py -t data/ciks.txt -o data/product_data/run.txt -e data/product_data/run_error_log.txt -p data/product_data/data_log.txt
```

Run all commands from the root of the repository.

## Outputs

- `data/product_data/run.txt` — product-to-firm mapping.
- `data/product_data/run_error_log.txt` — errors per CIK.
- `data/product_data/data_log.txt` — full pipeline object as a pickle.

## Inputs

The following inputs are required. Set their paths in `config.py` or in the command-line arguments.

| Dataset | Where to get | Config key / location |
|---|---|---|
| CIK list | Included in repo | `data/ciks.txt` |
| HS code reference | Included in repo | `data/HSCodeandDescription.xlsx` |
| SEC 10-K filings | Downloaded at runtime from SEC EDGAR | SEC identity in `config.py` |
| LLM inference | [OpenRouter](https://openrouter.ai/) API | `API_KEY` in `config.py` |

## Notes

### 10-K section scrapping tags 

`tenk_tag` records the outcome of the scrapping of the 10-K filings for every row. A row is accepted only when the filing's `period_of_report` belongs to the requested `fyear`; the surrounding years are searched only to find filings that were published after the fiscalyear end.

| Tag | Exact meaning | `tenk_section` | Retried on next run? |
|---|---|---|---|
| **`OK`** | A filing whose `period_of_report` belongs to `fyear` was read and an Item 1 Business section was extracted. | section text | No |
| **`Missing 10-K`** | No filing whose form starts with `10-K` was found in the `fyear - 1`, `fyear`, `fyear + 1` discovery window. This is about filing discovery, not download failure. | `NULL` | No |
| **`No 10-K for fyear`** | At least one 10-K variant was found, but none has a `period_of_report` belonging to `fyear`. The row is not filled from an adjacent year. | `NULL` | No |
| **`No Item 1 Business`** | At least one exact-year candidate filing was readable, but no candidate produced an Item 1 Business section. Typically the Business section is absent or only exists in an exhibit/amendment. | `NULL` | No |
| **`Document error`** | No exact-year candidate filing could be read as text, and at least one attempt raised an exception or returned empty. Examples: primary document 404, PDF/paper filing, decoding failure. | `NULL` | Yes |


### HS-Code extraction notes

- The HS-code reranking step, `parse_product_hs` in `src/llm_utilities.py`, uses the decision model `jaredpalmer/kev-4b` through OpenRouter's Decisions API. It asks Kev one yes/no question per candidate code and keeps every candidate whose probability of being correct is greater than or equal to `KEV_HS_THRESHOLD` (default `0.5`). If no candidate reaches the threshold, the product gets no HS code. These settings live in `config.py` and are documented in `example_config.py`; Kev-4B has an 8,192-token context.
- Semantic search uses `sentence-transformers/all-MiniLM-L6-v2` on Apple Silicon (`mps`). If you use CPU or CUDA, change the device in `src/semantic_search.py`.

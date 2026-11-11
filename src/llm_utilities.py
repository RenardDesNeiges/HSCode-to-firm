import os
import importlib.util
import re
import time
from dataclasses import dataclass, field
import pandas as pd
import requests
from openrouter import OpenRouter
import tiktoken
from deepseek_tokenizer import ds_token

CHEAP_MODEL = "deepseek/deepseek-v4-flash"
NICE_MODEL = "deepseek/deepseek-v4-flash"

# Load configuration from the repository root (one directory above this file).
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config_spec = importlib.util.spec_from_file_location(
    "config", os.path.join(_ROOT_DIR, "config.py")
)
_config_module = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config_module)
API_KEY = _config_module.API_KEY

PROMPT_DIR = os.path.join(_ROOT_DIR, "prompts")

MODEL = "CHEAP_MODEL"

# Decision-model settings for the HS-code reranking step. They can be
# overridden in config.py; the defaults work with an unmodified config file.
KEV_MODEL = getattr(_config_module, "KEV_MODEL", "jaredpalmer/kev-4b")
KEV_DECISIONS_URL = getattr(
    _config_module,
    "KEV_DECISIONS_URL",
    "https://openrouter.ai/api/alpha/decisions",
)
KEV_HS_THRESHOLD = float(getattr(_config_module, "KEV_HS_THRESHOLD", 0.5))


@dataclass(frozen=True)
class HSClassification:
    """Result of a Kev HS-code decision for one product.

    Attributes
    ----------
    codes:
        4-digit HS codes (strings, leading zeros preserved) whose probability
        of being a correct classification reached ``threshold``, sorted
        ascending. Empty when no candidate reaches the threshold.
    probabilities:
        Probability that each candidate code is a correct classification.
    threshold:
        Probability threshold used to select ``codes``.
    model:
        Exact decision-model checkpoint that produced the probabilities.
    """

    codes: tuple[str, ...] = ()
    probabilities: dict[str, float] = field(default_factory=dict)
    threshold: float = KEV_HS_THRESHOLD
    model: str = KEV_MODEL

    def __str__(self):
        return ", ".join(self.codes)

    def __bool__(self):
        return bool(self.codes)


def secure_sec_length(str):
    if max(count_tokens_deepseek(str),count_tokens_qwen(str)) > 26000:
        return str[:32000*3]
    else:
       return str

def count_tokens_qwen(text):
    """Count tokens for Qwen2.5 models using the cl100k_base encoding."""
    encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def count_tokens_deepseek(text):
    """Count tokens for DeepSeek V4 models using the official tokenizer."""
    result = ds_token.encode(text)
    return len(result)

def query_a_model_openai(prompt, model_name=CHEAP_MODEL):
   with OpenRouter(api_key=API_KEY) as client:
      response = client.chat.send(
         model=model_name,
         messages=[
               {"role": "user", "content": prompt}
         ],
      )
      return response.choices[0].message.content

def generate_search_terms(product):
   prompt_path = os.path.join(PROMPT_DIR, "generate_search_terms.txt")
   with open(prompt_path, "r") as f:
    prompt_template = f.read()

   hscodes = pd.read_excel("data/HSCodeandDescription.xlsx",sheet_name='HS22')
   hs_chapters = hscodes[hscodes['Level']==2]['Description'].to_string(index=False, header=False)

   prompt = prompt_template.replace("{query}", product).replace("{hs_chapters}", hs_chapters)
   search_terms = query_a_model_openai(prompt, CHEAP_MODEL)
   return search_terms


def refine_product_list(first_guess_list,context):
   prompt_path = os.path.join(PROMPT_DIR, "cleanup_the_data.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{context}', context).\
      replace('{list}',first_guess_list)
   out = query_a_model_openai(prompt,NICE_MODEL)
   to_remove = {e for e in out.replace(' ','').split(',') if e.isdigit()}
   return to_remove

def parse_filing(filing):
   prompt_path = os.path.join(PROMPT_DIR, "parse_10_k_filing.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{filing}', filing)
   out = query_a_model_openai(prompt,NICE_MODEL)
   return out

def parse_filing_context(filing):
   prompt_path = os.path.join(PROMPT_DIR, "generate_context_10k.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{filing}', filing)
   out = query_a_model_openai(prompt,CHEAP_MODEL)
   return out


def query_decision_model(state, questions, model_name=KEV_MODEL, 
                         timeout=120, retries=3):
    payload = {"model": model_name, "state": state, "questions": questions}
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(retries):
        try:
            response = requests.post(
                KEV_DECISIONS_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue

        if response.ok:
            return response.json()

        # Retry transient upstream failures; surface everything else.
        if response.status_code in (408, 429, 500, 502, 503, 524, 529) \
                and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue

        raise RuntimeError(
            f"Decision-model request to {model_name} failed with HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )

    raise RuntimeError(f"Decision-model request to {model_name} failed")


_CANDIDATE_LINE = re.compile(r"^\s*(\d{4})\s+(\S.*?)\s*$")

_HS_GUIDANCE = """\
Classification guidance:
- Complete vehicles (cars, trucks, vans, buses) belong to headings 8701-8705.
- Choose a parts/accessories heading (for example 8708) only when the product is explicitly a part or an accessory.
- Use the company context to disambiguate: for example, wearables made by a technology company belong under 8517 or 8471, not under clothing headings.
- Judge every candidate independently: several candidates may be correct, and none may be."""


def _parse_candidates(candidates):
    parsed = []
    seen = set()
    for line in str(candidates).splitlines():
        match = _CANDIDATE_LINE.match(line)
        if not match:
            continue
        code, description = match.group(1), match.group(2).strip()
        if code in seen:
            continue
        seen.add(code)
        parsed.append((code, description))
    return parsed


def _build_hs_state(parsed_candidates, description, context):
    rows = "\n".join(f"{code} : {desc}" for code, desc in parsed_candidates)
    return (
        f"Product to classify: {description}\n\n"
        f"Company context: {context}\n\n"
        f"Candidate 4-digit HS codes:\n{rows}\n\n"
        f"{_HS_GUIDANCE}"
    )


def _build_hs_questions(parsed_candidates, description):
    return {
        code: {
            "type": "noul",
            "instructions": (
                f"Is HS code {code} a correct 4-digit HS classification "
                f"for the product '{description}'?"
            ),
            "criteria": {
                "true": (
                    "The candidate heading covers this product, given its "
                    "description and the company context."
                ),
                "false": (
                    "The candidate heading does not cover this product, given "
                    "its description and the company context."
                ),
            },
        }
        for code, _ in parsed_candidates
    }


def parse_product_hs(candidates, description, context, threshold=None):
    """Assign 4-digit HS codes to a product using Kev (``jaredpalmer/kev-4b``).

    Every candidate is posed to the decision model as an independent yes/no
    question. Candidates whose probability of being correct reaches
    ``threshold`` are kept; if none reaches it, the result is empty.

    Parameters
    ----------
    candidates:
        Candidate table as produced by ``DataFrame.to_string(index=False)``,
        with one 4-digit code and description per line.
    description:
        Natural-language description of the product.
    context:
        Company context, normally from ``parse_filing_context``.
    threshold:
        Probability threshold in ``[0, 1]``. Defaults to ``KEV_HS_THRESHOLD``
        (0.5, overridable in ``config.py``).

    Returns
    -------
    HSClassification
        Structured result with the selected codes and the probability of every
        candidate. ``str(result)`` yields the old comma-separated format.
    """
    threshold = KEV_HS_THRESHOLD if threshold is None else float(threshold)
    parsed_candidates = _parse_candidates(candidates)
    if not parsed_candidates:
        return HSClassification(probabilities={}, threshold=threshold)

    state = _build_hs_state(parsed_candidates, description, context)
    questions = _build_hs_questions(parsed_candidates, description)
    response = query_decision_model(state, questions, model_name=KEV_MODEL)

    answers = response.get("answers") or {}
    probabilities = {
        code: float((answers.get(code) or {}).get("noul", 0.0))
        for code, _ in parsed_candidates
    }
    selected = tuple(
        sorted(code for code, probability in probabilities.items()
               if probability >= threshold)
    )
    return HSClassification(
        codes=selected,
        probabilities=probabilities,
        threshold=threshold,
        model=response.get("model", KEV_MODEL),
    )

import os
import importlib.util
import pandas as pd
import requests
from openrouter import OpenRouter
import tiktoken
from deepseek_tokenizer import ds_token

# Load configuration from the repository root (one directory above this file).
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config_spec = importlib.util.spec_from_file_location(
    "config", os.path.join(_ROOT_DIR, "config.py")
)
_config_module = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(_config_module)
API_KEY = _config_module.API_KEY

PROMPT_DIR = os.path.join(_ROOT_DIR, "prompts")

MODEL = "qwen2.5:7b"

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

def query_a_model_openai(prompt, model_name="qwen/qwen-2.5-7b-instruct"):
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
   search_terms = query_a_model_openai(prompt, "qwen/qwen-2.5-7b-instruct")
   return search_terms


def refine_product_list(first_guess_list,context):
   prompt_path = os.path.join(PROMPT_DIR, "cleanup_the_data.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{context}', context).\
      replace('{list}',first_guess_list)
   out = query_a_model_openai(prompt,"deepseek/deepseek-v4-flash")
   to_remove = {e for e in out.replace(' ','').split(',') if e.isdigit()}
   return to_remove

def parse_filing(filing):
   prompt_path = os.path.join(PROMPT_DIR, "parse_10_k_filing.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{filing}', filing)
   out = query_a_model_openai(prompt,"deepseek/deepseek-v4-flash")
   return out

def parse_filing_context(filing):
   prompt_path = os.path.join(PROMPT_DIR, "generate_context_10k.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{filing}', filing)
   out = query_a_model_openai(prompt,"qwen/qwen-2.5-7b-instruct")
   return out


def parse_product_hs(candidates, description,context):
   SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {"type": "integer"}
        }
    },
    "required": ["products"]
    }
   prompt_path = os.path.join(PROMPT_DIR, "reranker_hs_codes.txt")
   with open(prompt_path, 'r') as f:
      prompt_template = f.read()
   prompt = prompt_template.replace('{candidates}', candidates).replace('{description}', description).replace('{context}',context)
   
   out = query_a_model_openai(prompt,"qwen/qwen-2.5-7b-instruct")
   return out

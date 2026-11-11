# Copy this file to config.py and fill in your own values.
# config.py is gitignored and should never be committed.

# OpenRouter API key used by src/llm_utilities.py
API_KEY = 'your-openrouter-api-key-here'

# SEC EDGAR identity settings used by src/sec_utilities.py.
# These are required by the SEC when downloading filings.
SEC_NAME = "Your Name"
SEC_EMAIL = "your.email@example.com"

# Optional: decision-model settings for the HS-code reranking step.
# Kev is a decision model (not a chat model), so it is called through
# OpenRouter's Decisions API instead of the chat-completions endpoint.
KEV_MODEL = "jaredpalmer/kev-4b"
KEV_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
# Keep a candidate code when P(correct) >= KEV_HS_THRESHOLD.
# If no candidate reaches it, the product gets no HS code.
KEV_HS_THRESHOLD = 0.5

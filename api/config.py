import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _secret(name: str) -> str:
    """A secret from the environment; an unreplaced REPLACE_ME placeholder counts as unset."""
    v = os.getenv(name, "")
    return "" if "REPLACE_ME" in v else v


FALKORDB_HOST =os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = _int("FALKORDB_PORT", 6380)
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None
POOL_SIZE = _int("FALKORDB_POOL_SIZE", 16)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_API_KEY = _secret("LLM_API_KEY")
MODEL_STRONG = os.getenv("MODEL_STRONG", "gpt-4.1")
MODEL_FAST = os.getenv("MODEL_FAST", "gpt-4.1-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = _int("EMBED_DIM", 1536)

# PRD says 0.75, but a playbook created from one success weighs (1+1)/(1+2) = 0.667, so incident #2 could never take
# the fast path. Tune on Day 3 against the story set.
FAST_PATH_WEIGHT = float(os.getenv("FAST_PATH_WEIGHT") or 0.65)
# The PRD also wants the playbook's fault_class to "match", but the crew is never told the class, so instead the
# playbook must come from a past incident whose STRUCTURAL similarity (symptom kinds, triage-time signals, shared
# dependencies; not recency or text) is at least this. Gating on the total recall score let unrelated fault classes in,
# because recency and common words inflate it. 0.69 is derived, not fitted: symptom kinds (0.35) and triage-time
# signals (0.35) together weigh 0.70, so this means "both class-level features match"; a class mismatch can score at
# most 0.65 (one feature plus domain, tier and shared dependencies).
FAST_PATH_MIN_STRUCTURE = float(os.getenv("FAST_PATH_MIN_STRUCTURE") or 0.69)

DEMO_TENANT_KEY =_secret("DEMO_TENANT_KEY")
RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 10)
MAX_TOKENS_PER_INCIDENT = _int("MAX_TOKENS_PER_INCIDENT", 60000)

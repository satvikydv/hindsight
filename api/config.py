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
# playbook must come from a past incident whose recall score is at least this. Tune on Day 3.
FAST_PATH_MIN_SIMILARITY = float(os.getenv("FAST_PATH_MIN_SIMILARITY") or 0.45)

DEMO_TENANT_KEY =_secret("DEMO_TENANT_KEY")
RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 10)
MAX_TOKENS_PER_INCIDENT = _int("MAX_TOKENS_PER_INCIDENT", 60000)

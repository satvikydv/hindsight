import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = _int("FALKORDB_PORT", 6380)
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None
POOL_SIZE = _int("FALKORDB_POOL_SIZE", 16)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
MODEL_STRONG = os.getenv("MODEL_STRONG", "claude-sonnet-5-5")
MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-5-5")
EMBED_MODEL = os.getenv("EMBED_MODEL", "")
EMBED_DIM = _int("EMBED_DIM", 1536)

DEMO_TENANT_KEY = os.getenv("DEMO_TENANT_KEY", "")
RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 10)
MAX_TOKENS_PER_INCIDENT = _int("MAX_TOKENS_PER_INCIDENT", 60000)

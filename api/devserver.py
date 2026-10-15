"""The real API wired to the scripted expert policy instead of OpenAI: costs nothing and needs no key.

    uv run uvicorn api.devserver:app --port 8000

For developing the console and for a dry run of the demo flow. It exercises the same coordinator, graph, simulator and
HTTP routes as production; only the "model" is the deterministic policy from the benchmark, so what you see in the
timeline shows the plumbing, not how the real agents behave. Each call sleeps briefly (DEV_LLM_DELAY seconds) so the
live states are visible. Vector recall uses a hashing embedder, so its indexes are 64-wide: tenants made here are
throwaway and separate from the ones the production app creates.
"""

import os
import time

from api import config
from api.bench.scripted import expert_llm
from api.llm import FakeEmbedder
from api.main import create_app

DIM = 64
config.EMBED_DIM = DIM          # the vector index must match the embedder in use
_DELAY = float(os.getenv("DEV_LLM_DELAY", "0.7"))


class SlowScripted:
    def __init__(self):
        self.inner = expert_llm()

    def chat(self, tier, messages, tools):
        time.sleep(_DELAY)
        return self.inner.chat(tier, messages, tools)


app = create_app(llm=SlowScripted(), embedder=FakeEmbedder(DIM), resume=False, demo=False)

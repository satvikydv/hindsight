# Hindsight console

React + Vite + TypeScript. Cytoscape draws the graph, Recharts the benchmark curves.

```
npm install
npm run dev        # http://localhost:5173, proxies /api to http://localhost:8000
npm test           # parser and timeline unit tests
npm run build      # type-checks, then bundles to dist/
```

The backend must be running. Two ways to start it, from the repository root:

```
uv run uvicorn api.main:app --port 8000      # real model: needs LLM_API_KEY, spends money per incident
uv run uvicorn api.devserver:app --port 8000 # scripted policy: free, no key, for UI work and dry runs
```

For a deployed build set `VITE_API_URL` to the backend's URL (and add the console's origin to `CORS_ORIGINS`).
To point the dev proxy elsewhere, set `HINDSIGHT_API`.

Pages: **Console** (inject a fault, live timeline, graph with the cited path highlighted), **Playbooks** (what was
learned), **Benchmarks** (saved reports from `docs/results/`, no tokens spent per visitor). The benchmark page does not
need a tenant key; the others do.

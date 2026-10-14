"""Index creation for a tenant graph. Idempotent: re-running on an existing graph is a no-op."""

from api import config
from api.graph.client import TenantGraph

RANGE_INDEXES = [
    "CREATE INDEX FOR (s:Service) ON (s.name)",
    "CREATE INDEX FOR (s:Service) ON (s.id)",
    "CREATE INDEX FOR (i:Incident) ON (i.id)",
    "CREATE INDEX FOR (i:Incident) ON (i.status)",
    "CREATE INDEX FOR (a:Action) ON (a.id)",
    "CREATE INDEX FOR (d:Deploy) ON (d.at)",
    "CREATE INDEX FOR (d:Deploy) ON (d.id)",
    "CREATE INDEX FOR (p:Playbook) ON (p.id)",
    "CREATE INDEX FOR (p:Playbook) ON (p.fault_class)",
    "CREATE INDEX FOR (t:Team) ON (t.name)",
    "CREATE INDEX FOR (p:Person) ON (p.name)",
    "CREATE INDEX FOR (f:Fact) ON (f.id)",
    "CREATE INDEX FOR (d:Datastore) ON (d.id)",
    "CREATE INDEX FOR (d:Datastore) ON (d.name)",
    "CREATE INDEX FOR (c:ConfigFlag) ON (c.id)",
    "CREATE INDEX FOR (c:ConfigFlag) ON (c.key)",
    "CREATE INDEX FOR (h:Hypothesis) ON (h.id)",
    "CREATE INDEX FOR (h:Hypothesis) ON (h.incident_id)",
    "CREATE INDEX FOR (s:Symptom) ON (s.id)",
    "CREATE INDEX FOR (m:Message) ON (m.id)",
    "CREATE INDEX FOR (s:Step) ON (s.id)",
    "CREATE INDEX FOR (p:Person) ON (p.id)",
    "CREATE INDEX FOR (t:Team) ON (t.id)",
    "CREATE INDEX FOR (a:Agent) ON (a.name)",
]


def vector_indexes(dim: int) -> list[str]:
    opts = f"OPTIONS {{dimension:{dim}, similarityFunction:'cosine'}}"
    return [
        f"CREATE VECTOR INDEX FOR (i:Incident) ON (i.signature_embedding) {opts}",
        f"CREATE VECTOR INDEX FOR (f:Fact) ON (f.embedding) {opts}",
    ]


def init_graph(g: TenantGraph, embed_dim: int | None = None) -> None:
    for stmt in RANGE_INDEXES + vector_indexes(embed_dim or config.EMBED_DIM):
        try:
            g.write(stmt)
        except Exception as e:  # FalkorDB errors when the index already exists
            if "already indexed" not in str(e).lower() and "already exists" not in str(e).lower():
                raise

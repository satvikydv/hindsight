"""The 16 core Cypher queries (PRD §8), as named constants. These are the queries the README lists.

Conventions: parameters use $name; current-state edge reads filter `valid_to IS NULL`;
reads run via ro_query, writes via query.
"""

# Q1 Blast radius: who is hurt if this service fails (callers up to 4 hops, current edges only)
Q1_BLAST_RADIUS = """
MATCH (origin:Service {name: $svc})
MATCH p = (caller:Service)-[:DEPENDS_ON*1..4]->(origin)
WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL)
RETURN caller.name AS service, caller.tier AS tier, min(length(p)) AS hops
ORDER BY hops, tier
"""

# Q2 Recent changes near the failure: deploys on the origin or anything it depends on (<= 2 hops)
Q2_RECENT_CHANGES = """
MATCH (origin:Service {name: $svc})-[:DEPENDS_ON*0..2]->(s)
MATCH (d:Deploy)-[:DEPLOYED]->(s)
WHERE d.at >= $since
OPTIONAL MATCH (a:Person)-[:AUTHORED]->(d)
RETURN d.id AS deploy, d.version AS version, s.name AS service, d.at AS at,
       a.name AS author, d.flags_changed AS flags
ORDER BY d.at DESC LIMIT 10
"""

# Q3 Centrality, refreshed after topology changes
Q3_PAGERANK = """
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
SET node.pagerank = score
"""

# Q4 Structural recall: past resolved incidents with the same shape (symptom overlap, domain, tier, cause type).
# ROOT_CAUSE is OPTIONAL (the PRD requires it): the current cause is unknown at recall time so it is only a hint, and an
# incident whose Investigator never confirmed a cause must still be recallable by its shape.
Q4_STRUCTURAL_RECALL = """
MATCH (cur:Incident {id: $iid})-[:SHOWED]->(cs:Symptom)
WITH cur, collect(DISTINCT cs.kind) AS curKinds
MATCH (cur)-[:AFFECTS {role: 'origin'}]->(co:Service)
MATCH (past:Incident)-[:AFFECTS {role: 'origin'}]->(po:Service)
WHERE past.status = 'resolved' AND past.id <> cur.id
MATCH (past)-[:SHOWED]->(ps:Symptom)
WITH cur, curKinds, co, past, po, collect(DISTINCT ps.kind) AS pastKinds
OPTIONAL MATCH (past)-[:ROOT_CAUSE]->(rc)
WITH past, po, co, labels(rc)[0] AS causeType,
     size([k IN curKinds WHERE k IN pastKinds]) AS overlap,
     size(curKinds) + size(pastKinds) AS total
RETURN past.id AS incident, causeType,
       toFloat(overlap) / (total - overlap) AS symptomJaccard,
       po.domain = co.domain AS sameDomain, po.tier = co.tier AS sameTier
ORDER BY symptomJaccard DESC LIMIT 20
"""

# Q5 Vector recall over incident signatures (top 10)
Q5_VECTOR_RECALL = """
CALL db.idx.vector.queryNodes('Incident', 'signature_embedding', 10, vecf32($vec))
YIELD node, score
RETURN node.id AS incident, score
"""

# Q17 (added, not in the PRD) Direct dependencies of each incident's origin service, for the structural-recall
# "shared neighbours" term. One round trip for the current incident and all candidates.
Q17_ORIGIN_DEPENDENCIES = """
MATCH (i:Incident)-[:AFFECTS {role: 'origin'}]->(s:Service)-[r:DEPENDS_ON]->(d)
WHERE i.id IN $ids AND r.valid_to IS NULL
RETURN i.id AS incident, collect(d.name) AS deps
"""

# Q18 (added) The origin and everything it depends on within 2 hops, current edges only. Used for the Remediator's
# allowed scope and as the candidate set for suspect ranking.
Q18_NEIGHBOURHOOD = """
MATCH p = (o:Service {name: $svc})-[:DEPENDS_ON*0..2]->(n)
WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL)
RETURN n.name AS name, labels(n)[0] AS label, n.id AS id, n.tier AS tier, n.pagerank AS pagerank,
       min(length(p)) AS hops
ORDER BY hops, name
"""

# Q19 (added) How often each service was the root cause of past resolved incidents (the suspect score's history term).
# A Deploy counts for the service it was deployed on, a ConfigFlag for the service that has it.
Q19_PAST_CAUSE_COUNTS = """
MATCH (i:Incident {status: 'resolved'})-[:ROOT_CAUSE]->(rc)
OPTIONAL MATCH (rc)-[:DEPLOYED]->(ds:Service)
OPTIONAL MATCH (fs:Service)-[:HAS_FLAG]->(rc)
RETURN coalesce(ds.name, fs.name, rc.name) AS service, count(DISTINCT i) AS n
"""

# Q21 (added) Flag changes on a set of services since a time (a flag change is a new HAS_FLAG edge).
Q21_RECENT_FLAG_CHANGES = """
MATCH (s:Service)-[h:HAS_FLAG]->(f:ConfigFlag)
WHERE s.name IN $names AND h.valid_from >= $since
RETURN f.id AS flag_id, s.name AS service, f.key AS key, f.value AS value, h.valid_from AS at
ORDER BY at DESC
"""

# Q6 Candidate playbooks from similar incidents (active only, best weight first)
Q6_CANDIDATE_PLAYBOOKS = """
MATCH (i:Incident)-[:RESOLVED_BY]->(pb:Playbook)
WHERE i.id IN $similar_ids AND pb.status = 'active'
RETURN pb.id AS playbook, pb.name AS name, pb.fault_class AS fault_class,
       pb.weight AS weight, count(i) AS support
ORDER BY weight DESC, support DESC LIMIT 3
"""

# Q7 Playbook steps in order
Q7_PLAYBOOK_STEPS = """
MATCH (pb:Playbook {id: $pid})-[:HAS_STEP]->(s:Step)
RETURN s.order AS step, s.tool AS tool, s.args_template AS args,
       s.success_count AS wins, s.attempt_count AS tries
ORDER BY s.order
"""

# Q8 Owners (current)
Q8_OWNERS = """
MATCH (t:Team)-[o:OWNS]->(s:Service {name: $svc})
WHERE o.valid_to IS NULL
OPTIONAL MATCH (p:Person)-[m:MEMBER_OF]->(t)
WHERE m.valid_to IS NULL
RETURN t.name AS team, t.slack_channel AS channel, collect(p.name) AS members
"""

# Q9 Who knows this service: people ranked by incidents they worked on for it
Q9_WHO_KNOWS = """
MATCH (p:Person)-[w:WORKED_ON]->(i:Incident)-[:AFFECTS]->(s:Service {name: $svc})
RETURN p.name AS person, count(DISTINCT i) AS incidents, max(i.resolved_at) AS last_seen,
       collect(DISTINCT w.role) AS roles
ORDER BY incidents DESC, last_seen DESC LIMIT 5
"""

# Q10 Dependency path between two services
Q10_DEPENDENCY_PATH = """
MATCH (a:Service {name: $from}), (b:Service {name: $to})
RETURN shortestPath((a)-[:DEPENDS_ON*]->(b)) AS path
"""

# Q11 Explain an action: the nodes it CITED and the agent that took it
Q11_EXPLAIN_ACTION = """
MATCH (a:Action {id: $aid})
OPTIONAL MATCH (a)-[c:CITED]->(n)
OPTIONAL MATCH (a)-[:BY]->(ag:Agent)
RETURN a, ag.name AS agent, collect({why: c.why, node: n}) AS evidence
"""

# Q12 Facts about a node as of a time (temporal read)
Q12_FACTS_AS_OF = """
MATCH (f:Fact)-[:DERIVED_FROM|ABOUT]->(n {id: $node_id})
WHERE f.valid_from <= $t AND (f.valid_to IS NULL OR f.valid_to > $t)
RETURN f.text AS fact, f.source AS source, f.valid_from AS since
"""

# Q13 Reinforce a playbook (Laplace-smoothed weight)
Q13_REINFORCE_PLAYBOOK = """
MATCH (pb:Playbook {id: $pid})
SET pb.attempt_count = pb.attempt_count + 1,
    pb.success_count = pb.success_count + $won,
    pb.last_used_at = $now
SET pb.base_weight = (pb.success_count + 1.0) / (pb.attempt_count + 2.0)
SET pb.weight = pb.base_weight
RETURN pb.weight AS weight
"""

# Q14 Supersede ownership: close the current OWNS edge, open a new one (history kept)
Q14_SUPERSEDE_OWNERSHIP = """
MATCH (:Team)-[o:OWNS]->(s:Service {name: $svc})
WHERE o.valid_to IS NULL
SET o.valid_to = $now
WITH s
MATCH (t:Team {name: $team})
CREATE (t)-[:OWNS {valid_from: $now, source: $source}]->(s)
"""

# Q15 Acquire (or refresh, when the caller already holds it) the incident lock; no row back means another agent holds it
Q15_ACQUIRE_LOCK = """
MATCH (i:Incident {id: $iid})
WHERE i.lock_owner IS NULL OR i.lock_owner = $agent OR i.lock_until < $now
SET i.lock_owner = $agent, i.lock_until = $now + $ttl_ms
RETURN i.lock_owner AS owner
"""

# Q16 Handoff chain for an incident, in order
Q16_HANDOFF_CHAIN = """
MATCH (a:Agent)-[h:HANDOFF]->(b:Agent)
WHERE h.incident_id = $iid
RETURN a.name AS from, b.name AS to, h.summary AS summary, h.at AS at
ORDER BY h.seq
"""

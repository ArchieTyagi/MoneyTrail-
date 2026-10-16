"""
All the graph work lives here. Every function = one tool the agent can call.

Each function runs Cypher on FalkorDB and returns a small, readable dict.
When a function finds something suspicious it also returns `evidence`:
a list of paths with plain text ("A -> B -> C") and the node ids, so the
UI can highlight the exact path on the graph.

Graph model (see README for the picture):
  (:Person)-[:DIRECTOR_OF]->(:Company)
  (:Person)-[:RELATIVE_OF {relation}]->(:Person)
  (:Company)-[:REGISTERED_AT]->(:Address)
  (:Company|:Person)-[:HOLDS]->(:Account)
  (:Account)-[:TRANSFER {amount, date, ref}]->(:Account)
  (:Company)-[:INVOICED {invoice_no, amount, gst, date}]->(:Company)
  (:Case)-[:INVOLVES]->(any)            <- written by the agent
"""

import json
from datetime import datetime, timezone

from app.db import run

# ------------------------------------------------------------------ helpers


def inr(amount):
    """Format rupees the Indian way: 4500000 -> '₹45.0L', 25000000 -> '₹2.50Cr'."""
    if amount is None:
        return "₹0"
    a = float(amount)
    if a >= 1_00_00_000:
        return f"₹{a / 1_00_00_000:.2f}Cr"
    if a >= 1_00_000:
        return f"₹{a / 1_00_000:.1f}L"
    return f"₹{a:,.0f}"


def _label(n):
    return n.get("_label") or "?"


def path_text(path):
    """Turn a path into one readable line, e.g.
    Sharma Traders --paid ₹30.2L (2026-04-20)--> Kavya Supplies --paid ₹23.0L (2026-04-23)--> Rajesh Sharma"""
    nodes = {n["id"]: n for n in path["nodes"]}
    order = [n["id"] for n in path["nodes"]]
    parts = [nodes[order[0]]["name"]]
    for i, e in enumerate(path["edges"]):
        nxt = nodes[order[i + 1]]["name"]
        t = e["_type"]
        forward = e.get("from") == order[i]
        if t == "TRANSFER":
            verb = f"paid {inr(e.get('amount'))} ({e.get('date')})"
        elif t == "INVOICED":
            verb = f"invoiced {inr(e.get('amount'))} ({e.get('date')})"
        elif t == "RELATIVE_OF":
            verb = f"is {e.get('relation')} of" if forward else f"has {e.get('relation')}"
        elif t == "DIRECTOR_OF":
            verb = "is director of" if forward else "has director"
        elif t == "REGISTERED_AT":
            verb = "is registered at" if forward else "also houses"
        elif t == "HOLDS":
            verb = "holds account" if forward else "belongs to"
        else:
            verb = t.lower()
        parts.append(f"--{verb}--> {nxt}")
    return " ".join(parts)


def directed(path):
    """FalkorDB can hand back a directed path listed back-to-front. Flip it so it reads in the
    direction the money / invoices actually moved."""
    order = [n["id"] for n in path["nodes"]]
    edges = path["edges"]
    if edges and all(e.get("from") == order[i + 1] and e.get("to") == order[i] for i, e in enumerate(edges)):
        return {"nodes": path["nodes"][::-1], "edges": edges[::-1]}
    return path


def _edge_key(e):
    return (e.get("from"), e.get("to"), e.get("date"), e.get("amount"))


def dedupe_by_last_hop(paths):
    """Several routes can end in the same final payment. Keep one route per final payment
    (the one whose first payment is closest in time), so amounts are not counted twice."""
    best = {}
    for p in paths:
        k = _edge_key(p["edges"][-1])
        if k not in best or (p["edges"][0].get("date") or "") > (best[k]["edges"][0].get("date") or ""):
            best[k] = p
    return sorted(best.values(), key=lambda p: p["edges"][-1].get("date") or "")


def evidence_from_path(path):
    return {"text": path_text(path), "node_ids": [n["id"] for n in path["nodes"]]}


def is_chronological(path):
    """A real money trail moves forward in time. Drop paths that go backwards."""
    dates = [e.get("date") for e in path["edges"] if e.get("date")]
    return all(dates[i] <= dates[i + 1] for i in range(len(dates) - 1))


def entity_type(entity_id):
    r = run("MATCH (n {id:$id}) RETURN labels(n)[0] AS type, n.name AS name", {"id": entity_id}, read_only=True)
    return (r[0]["type"], r[0]["name"]) if r else (None, None)


def accounts_of(entity_id):
    """Bank accounts for a company/person. If the id is already an account, use it."""
    t, _ = entity_type(entity_id)
    if t == "Account":
        return [entity_id]
    r = run("MATCH ({id:$id})-[:HOLDS]->(a:Account) RETURN a.id AS id", {"id": entity_id}, read_only=True)
    return [x["id"] for x in r]


def _unique_cycles(paths):
    """Many paths can be the same loop (parallel invoices, different start points). Keep one per loop."""
    seen, out = set(), []
    for p in paths:
        key = tuple(sorted(n["id"] for n in p["nodes"]))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ------------------------------------------------------------------ look around


def search_entity(query: str):
    """Find companies, people, accounts or addresses by (part of) their name."""
    rows = run("""
        MATCH (n)
        WHERE (n:Company OR n:Person OR n:Account OR n:Address)
          AND toLower(n.name) CONTAINS toLower($q)
        RETURN labels(n)[0] AS type, n.id AS id, n.name AS name
        LIMIT 10""", {"q": query.strip()}, read_only=True)
    if not rows:
        # try each word on its own, "sharma traders" -> "sharma"
        for word in query.split():
            if len(word) > 3:
                rows = search_entity(word)["matches"]
                if rows:
                    break
    return {"matches": rows}


def entity_profile(entity_id: str):
    """Who/what is this, and who does it deal with? One hop around the entity."""
    t, name = entity_type(entity_id)
    if not t:
        return {"error": f"no entity with id {entity_id}"}
    out = {"id": entity_id, "type": t, "name": name}

    if t == "Company":
        c = run("""
            MATCH (c:Company {id:$id})
            OPTIONAL MATCH (p:Person)-[:DIRECTOR_OF]->(c)
            OPTIONAL MATCH (c)-[:REGISTERED_AT]->(a:Address)
            OPTIONAL MATCH (c)-[:HOLDS]->(acc:Account)
            RETURN c.gstin AS gstin, c.city AS city, c.incorporated_on AS incorporated_on,
                   c.is_client AS is_client,
                   collect(DISTINCT p {.id, .name}) AS directors,
                   collect(DISTINCT a {.id, .name}) AS address,
                   collect(DISTINCT acc {.id, .name}) AS accounts""", {"id": entity_id}, read_only=True)[0]
        out.update(c)
        out["other_companies_at_same_address"] = run("""
            MATCH (c:Company {id:$id})-[:REGISTERED_AT]->(:Address)<-[:REGISTERED_AT]-(o:Company)
            WHERE o.id <> c.id
            RETURN o.id AS id, o.name AS name""", {"id": entity_id}, read_only=True)
        out["sold_to"] = run("""
            MATCH (c:Company {id:$id})-[i:INVOICED]->(b:Company)
            RETURN b.id AS id, b.name AS name, count(i) AS invoices, sum(i.amount) AS total
            ORDER BY total DESC LIMIT 5""", {"id": entity_id}, read_only=True)
        out["bought_from"] = run("""
            MATCH (s:Company)-[i:INVOICED]->(c:Company {id:$id})
            RETURN s.id AS id, s.name AS name, count(i) AS invoices, sum(i.amount) AS total
            ORDER BY total DESC LIMIT 5""", {"id": entity_id}, read_only=True)

    elif t == "Person":
        p = run("""
            MATCH (p:Person {id:$id})
            OPTIONAL MATCH (p)-[:DIRECTOR_OF]->(c:Company)
            OPTIONAL MATCH (p)-[:HOLDS]->(acc:Account)
            RETURN p.pan AS pan, p.city AS city,
                   collect(DISTINCT c {.id, .name}) AS director_of,
                   collect(DISTINCT acc {.id, .name}) AS accounts""", {"id": entity_id}, read_only=True)[0]
        out.update(p)
        out["relatives"] = run("""
            MATCH (p:Person {id:$id})-[r:RELATIVE_OF]-(o:Person)
            OPTIONAL MATCH (o)-[:DIRECTOR_OF]->(c:Company)
            RETURN o.id AS id, o.name AS name, r.relation AS relation,
                   collect(c.name) AS director_of""", {"id": entity_id}, read_only=True)

    elif t == "Address":
        out["companies_registered_here"] = run("""
            MATCH (c:Company)-[:REGISTERED_AT]->(:Address {id:$id})
            RETURN c.id AS id, c.name AS name, c.incorporated_on AS incorporated_on""",
            {"id": entity_id}, read_only=True)
        return out

    elif t == "Account":
        out["holder"] = run("MATCH (h)-[:HOLDS]->(:Account {id:$id}) RETURN h.id AS id, h.name AS name, labels(h)[0] AS type",
                            {"id": entity_id}, read_only=True)

    # Money in and out, grouped by who is on the other side
    accs = accounts_of(entity_id)
    out["paid_to"] = run("""
        MATCH (a:Account)-[t:TRANSFER]->(b:Account)<-[:HOLDS]-(h)
        WHERE a.id IN $accs
        RETURN h.id AS id, h.name AS name, labels(h)[0] AS type,
               count(t) AS payments, sum(t.amount) AS total
        ORDER BY total DESC LIMIT 6""", {"accs": accs}, read_only=True)
    out["received_from"] = run("""
        MATCH (h)-[:HOLDS]->(b:Account)-[t:TRANSFER]->(a:Account)
        WHERE a.id IN $accs
        RETURN h.id AS id, h.name AS name, labels(h)[0] AS type,
               count(t) AS payments, sum(t.amount) AS total
        ORDER BY total DESC LIMIT 6""", {"accs": accs}, read_only=True)
    for k in ("paid_to", "received_from", "sold_to", "bought_from"):
        for row in out.get(k, []):
            row["total"] = inr(row["total"])
    out["prior_cases"] = len(get_prior_cases(entity_id)["cases"])
    return out


# ------------------------------------------------------------------ loops


def find_invoice_cycles(company_id: str, max_hops: int = 6):
    """GST circular trading: invoices that go around in a loop and come back to the same company."""
    max_hops = max(2, min(int(max_hops), 8))
    paths = run(f"""
        MATCH p=(c:Company {{id:$id}})-[:INVOICED*2..{max_hops}]->(c)
        RETURN p LIMIT 300""", {"id": company_id}, read_only=True)
    cycles = _unique_cycles([directed(r["p"]) for r in paths])
    results = []
    for cyc in cycles[:3]:
        ids = [n["id"] for n in cyc["nodes"]]
        names = {n["id"]: n["name"] for n in cyc["nodes"]}
        hops = []
        for i in range(len(ids) - 1):
            r = run("""
                MATCH (s:Company {id:$s})-[i:INVOICED]->(b:Company {id:$b})
                RETURN count(i) AS invoices, sum(i.amount) AS total, min(i.date) AS first, max(i.date) AS last""",
                {"s": ids[i], "b": ids[i + 1]}, read_only=True)[0]
            hops.append({"from": names[ids[i]], "to": names[ids[i + 1]], "invoices": r["invoices"],
                         "total": inr(r["total"]), "first": r["first"], "last": r["last"]})
        results.append({"loop": " -> ".join(names[i] for i in ids), "hops": hops,
                        "rounds": min(h["invoices"] for h in hops), "evidence": evidence_from_path(cyc)})
    return {"company_id": company_id, "cycles_found": len(cycles), "cycles": results}


def find_money_cycles(entity_id: str, max_hops: int = 5):
    """Money that leaves an account and comes back to the same account, in time order."""
    max_hops = max(2, min(int(max_hops), 7))
    accs = accounts_of(entity_id)
    paths = run(f"""
        MATCH (a:Account) WHERE a.id IN $accs
        MATCH p=(a)-[:TRANSFER*2..{max_hops}]->(a)
        RETURN p LIMIT 300""", {"accs": accs}, read_only=True)
    paths = [p for p in (directed(r["p"]) for r in paths) if is_chronological(p)]
    cycles = _unique_cycles(paths)
    return {"entity_id": entity_id, "cycles_found": len(cycles),
            "cycles": [evidence_from_path(c) for c in cycles[:3]]}


def find_round_tripping(company_id: str, max_hops: int = 4):
    """Money that leaves the company through someone else and lands with its own directors or their family.
    Direct salary (company -> director) is ignored, we only look at routes with a middle party."""
    max_hops = max(2, min(int(max_hops), 6))
    paths = run(f"""
        MATCH (c:Company {{id:$id}})-[:HOLDS]->(src:Account)
        MATCH (c)<-[:DIRECTOR_OF]-(d:Person)
        OPTIONAL MATCH (d)-[:RELATIVE_OF]-(r:Person)
        WITH src, collect(DISTINCT d) + collect(DISTINCT r) AS people
        UNWIND people AS person
        MATCH (person)-[:HOLDS]->(dst:Account)
        MATCH p=(src)-[:TRANSFER*2..{max_hops}]->(dst)
        RETURN p LIMIT 200""", {"id": company_id}, read_only=True)
    paths = [p for p in (directed(r["p"]) for r in paths) if is_chronological(p)]
    paths = dedupe_by_last_hop(paths)
    total = sum(p["edges"][-1].get("amount", 0) for p in paths)
    ev = []
    for p in paths[:6]:
        last_acc = p["nodes"][-1]["id"]
        holder = run("MATCH (h:Person)-[:HOLDS]->(:Account {id:$a}) RETURN h.name AS name", {"a": last_acc}, read_only=True)
        e = evidence_from_path(p)
        e["ends_with"] = holder[0]["name"] if holder else None
        ev.append(e)
    return {"company_id": company_id, "routes_found": len(paths),
            "amount_returned_to_insiders": inr(total), "routes": ev}


# ------------------------------------------------------------------ links between parties


def explain_link(entity_a: str, entity_b: str, max_hops: int = 5):
    """How are two parties connected through people, family or a shared address (not money)?"""
    max_hops = max(1, min(int(max_hops), 6))
    rows = run(f"""
        MATCH (a {{id:$a}}), (b {{id:$b}})
        MATCH p=(a)-[:DIRECTOR_OF|RELATIVE_OF|REGISTERED_AT|HOLDS*1..{max_hops}]-(b)
        RETURN p, length(p) AS len ORDER BY len LIMIT 3""", {"a": entity_a, "b": entity_b}, read_only=True)
    if not rows:
        return {"linked": False, "note": f"no ownership, family or address link within {max_hops} hops"}
    return {"linked": True, "shortest_link_hops": rows[0]["len"],
            "links": [evidence_from_path(r["p"]) for r in rows]}


def find_related_counterparties(company_id: str):
    """Check everyone this company trades with: are any of them secretly connected to it
    through directors, family or a shared address? (Related-party transactions.)"""
    cps = run("""
        MATCH (c:Company {id:$id})-[:HOLDS]->(:Account)-[t:TRANSFER]-(:Account)<-[:HOLDS]-(v:Company)
        RETURN v.id AS id, v.name AS name, sum(t.amount) AS money
        UNION
        MATCH (c:Company {id:$id})-[i:INVOICED]-(v:Company)
        RETURN v.id AS id, v.name AS name, sum(i.amount) AS money""", {"id": company_id}, read_only=True)
    merged = {}
    for r in cps:
        m = merged.setdefault(r["id"], {"id": r["id"], "name": r["name"], "volume": 0})
        m["volume"] += r["money"] or 0
    flagged = []
    for cp in merged.values():
        link = explain_link(company_id, cp["id"], 4)
        if link["linked"]:
            flagged.append({"counterparty": cp["name"], "id": cp["id"], "trade_volume": inr(cp["volume"]),
                            "link": link["links"][0]})
    return {"company_id": company_id, "counterparties_checked": len(merged),
            "related_counterparties": flagged}


def trace_money(from_id: str, to_id: str, max_hops: int = 4):
    """Follow money from one party to another, only along routes that move forward in time.
    Several parallel routes of similar size = money was split up (layering)."""
    max_hops = max(1, min(int(max_hops), 6))
    rows = run(f"""
        MATCH (s:Account) WHERE s.id IN $src
        MATCH (t:Account) WHERE t.id IN $dst
        MATCH p=(s)-[:TRANSFER*1..{max_hops}]->(t)
        RETURN p LIMIT 200""", {"src": accounts_of(from_id), "dst": accounts_of(to_id)}, read_only=True)
    paths = [p for p in (directed(r["p"]) for r in rows) if is_chronological(p)]
    paths = dedupe_by_last_hop(paths)
    if not paths:
        return {"routes_found": 0, "note": "no time-ordered money route found"}
    arrived = sum(p["edges"][-1].get("amount", 0) for p in paths)
    middle = sorted({n["name"] for p in paths for n in p["nodes"][1:-1]})
    return {"routes_found": len(paths), "total_arrived": inr(arrived),
            "middle_accounts": middle[:10],
            "looks_like_split_payment": len(paths) >= 3 and len(middle) >= 3,
            "routes": [evidence_from_path(p) for p in paths[:6]]}


# ------------------------------------------------------------------ graph algorithms


def find_hub_accounts(top_k: int = 5):
    """Accounts that a lot of money passes THROUGH (betweenness centrality), with how many
    different parties pay into them and how much leaves again (pass-through)."""
    rows = run("""
        CALL algo.betweenness({nodeLabels:['Account'], relationshipTypes:['TRANSFER']})
        YIELD node, score
        WITH node, score ORDER BY score DESC LIMIT 15
        MATCH (h)-[:HOLDS]->(node)
        OPTIONAL MATCH (s:Account)-[tin:TRANSFER]->(node)
        WITH node, score, h, count(DISTINCT s) AS senders, sum(tin.amount) AS inflow
        OPTIONAL MATCH (node)-[tout:TRANSFER]->(:Account)
        RETURN node.id AS account_id, node.name AS account, h.id AS holder_id, labels(h)[0] AS holder_type,
               h.incorporated_on AS incorporated_on, score AS betweenness,
               senders, inflow, sum(tout.amount) AS outflow""", read_only=True)
    for r in rows:
        r["pass_through_ratio"] = round((r["outflow"] or 0) / r["inflow"], 2) if r["inflow"] else 0
        # simple, explainable score: central + many payers + money just passes through
        r["hub_score"] = round(r["betweenness"] * (1 + r["senders"] / 5) *
                               (1 + min(r["pass_through_ratio"], 1.2)), 1)
        r["inflow"], r["outflow"] = inr(r["inflow"]), inr(r["outflow"])
        r["betweenness"] = round(r["betweenness"], 1)
    rows.sort(key=lambda r: r["hub_score"], reverse=True)
    return {"algorithm": "betweenness centrality on Account-TRANSFER graph", "top": rows[:max(1, int(top_k))]}


def find_control_groups(min_companies: int = 2):
    """Group companies that are tied together by common directors, family or the same address
    (weakly connected components). Then measure how much they trade WITH EACH OTHER.
    Heavy trade inside a group = possible related-party or circular trading."""
    groups = run("""
        CALL algo.WCC({nodeLabels:['Company','Person','Address'],
                       relationshipTypes:['DIRECTOR_OF','RELATIVE_OF','REGISTERED_AT']})
        YIELD node, componentId
        WITH componentId, collect(node) AS members
        WITH componentId, [m IN members WHERE m:Company] AS companies,
             [m IN members WHERE m:Person | m.name] AS people
        WHERE size(companies) >= $min
        RETURN componentId, [c IN companies | c.id] AS ids, [c IN companies | c.name] AS names, people""",
        {"min": int(min_companies)}, read_only=True)
    out = []
    for g in groups:
        inner = run("""
            MATCH (a:Company)-[i:INVOICED]->(b:Company) WHERE a.id IN $ids AND b.id IN $ids
            RETURN count(i) AS n, sum(i.amount) AS total""", {"ids": g["ids"]}, read_only=True)[0]
        money = run("""
            MATCH (a:Company)-[:HOLDS]->(:Account)-[t:TRANSFER]->(:Account)<-[:HOLDS]-(h)
            WHERE a.id IN $ids AND ((h:Company AND h.id IN $ids) OR (h:Person AND h.name IN $people))
            RETURN count(t) AS n, sum(t.amount) AS total""", {"ids": g["ids"], "people": g["people"]},
            read_only=True)[0]
        out.append({"companies": g["names"], "company_ids": g["ids"], "people": g["people"],
                    "invoices_inside_group": inner["n"], "invoice_value_inside_group": inr(inner["total"]),
                    "payments_inside_group": money["n"], "payment_value_inside_group": inr(money["total"]),
                    "_rank": (inner["total"] or 0) + (money["total"] or 0)})
    out.sort(key=lambda g: g["_rank"], reverse=True)
    for g in out:
        g.pop("_rank")
    return {"algorithm": "weakly connected components on ownership/family/address edges", "groups": out[:8]}


def find_shell_clusters(min_companies: int = 3):
    """Addresses where several companies are registered. Shell rings often share one shop or office."""
    rows = run("""
        MATCH (a:Address)<-[:REGISTERED_AT]-(c:Company)
        WITH a, collect(c) AS cs WHERE size(cs) >= $min
        RETURN a.id AS address_id, a.name AS address,
               [c IN cs | c.name] AS companies, [c IN cs | c.id] AS company_ids,
               [c IN cs | c.incorporated_on] AS incorporated_on""", {"min": int(min_companies)}, read_only=True)
    return {"clusters": rows}


def scan_portfolio():
    """Quick first pass over the whole graph: loops in invoices, tight company groups, hub accounts.
    Use it to find leads, then dig into each lead with the other tools."""
    loops = run("""
        MATCH p=(c:Company)-[:INVOICED*2..5]->(c)
        RETURN p LIMIT 300""", read_only=True)
    loops = _unique_cycles([directed(r["p"]) for r in loops])
    return {
        "invoice_loops": [" -> ".join(n["name"] for n in p["nodes"]) for p in loops[:5]],
        "top_groups": find_control_groups(2)["groups"][:4],
        "top_hub_accounts": [{"account": h["account"], "holder_id": h["holder_id"], "hub_score": h["hub_score"],
                              "senders": h["senders"], "pass_through_ratio": h["pass_through_ratio"]}
                             for h in find_hub_accounts(3)["top"]],
        "shared_addresses": find_shell_clusters(3)["clusters"],
    }


# ------------------------------------------------------------------ memory + actions


def get_prior_cases(entity_id: str):
    """Has the firm already opened a case on this entity in an earlier session?"""
    rows = run("""
        MATCH (k:Case)-[:INVOLVES]->({id:$id})
        RETURN k.id AS id, k.title AS title, k.risk_score AS risk_score, k.status AS status,
               k.created_at AS created_at, k.summary AS summary
        ORDER BY k.created_at DESC""", {"id": entity_id}, read_only=True)
    return {"entity_id": entity_id, "cases": rows}


def open_case(title: str, risk_score: int, summary: str, entity_ids: list,
              evidence: list, client_query_note: str, recommended_action: str):
    """Write a finding back into the graph as a Case node linked to every entity involved."""
    n = run("MATCH (k:Case) RETURN count(k) AS n")[0]["n"]
    case_id = f"CASE-{n + 1:03d}"
    risk = max(0, min(int(risk_score), 100))
    level = "high" if risk >= 75 else "medium" if risk >= 40 else "low"
    run("""
        CREATE (k:Case {id:$id, name:$title, title:$title, risk_score:$risk, risk_level:$level,
                        summary:$summary, evidence:$evidence, client_query_note:$note,
                        recommended_action:$action, status:'open', created_at:$now})""",
        {"id": case_id, "title": title, "risk": risk, "level": level, "summary": summary,
         "evidence": json.dumps(evidence), "note": client_query_note, "action": recommended_action,
         "now": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    linked = run("""
        MATCH (k:Case {id:$id}) MATCH (n) WHERE n.id IN $ids AND NOT n:Case
        CREATE (k)-[:INVOLVES]->(n) RETURN count(n) AS linked""", {"id": case_id, "ids": entity_ids})[0]
    return {"case_id": case_id, "risk_level": level, "linked_entities": linked["linked"], "status": "open"}


def escalate_case(case_id: str, reason: str):
    """Send a high-risk case to the partner for review."""
    r = run("""
        MATCH (k:Case {id:$id})
        SET k.status = 'escalated_to_partner', k.escalation_reason = $reason
        RETURN k.id AS id, k.status AS status""", {"id": case_id, "reason": reason})
    return r[0] if r else {"error": f"no case {case_id}"}


# ------------------------------------------------------------------ used by the UI, not the agent


def list_cases():
    rows = run("""
        MATCH (k:Case)
        OPTIONAL MATCH (k)-[:INVOLVES]->(n)
        WITH k, collect(n.name) AS involves
        RETURN k {.*} AS case, involves, k.created_at AS created
        ORDER BY created DESC""", read_only=True)
    out = []
    for r in rows:
        c = r["case"]
        c.pop("_label", None)
        try:
            c["evidence"] = json.loads(c.get("evidence") or "[]")
        except ValueError:
            pass
        c["involves"] = r["involves"]
        out.append(c)
    return out


def subgraph(ids: list, hops: int = 1, limit: int = 250):
    """Nodes around the given ids, for drawing. Parallel money/invoice edges are merged into one."""
    if not ids:
        rows = run("""
            MATCH (a)-[r]->(b)
            RETURN a {.id, .name} AS a, labels(a)[0] AS la, type(r) AS t, r.amount AS amt,
                   b {.id, .name} AS b, labels(b)[0] AS lb LIMIT $limit""", {"limit": limit}, read_only=True)
    else:
        rows = run(f"""
            MATCH (s) WHERE s.id IN $ids
            MATCH (s)-[*0..{int(hops)}]-(x)
            WITH collect(DISTINCT x) AS xs
            UNWIND xs AS a
            MATCH (a)-[r]->(b) WHERE b IN xs
            RETURN a {{.id, .name}} AS a, labels(a)[0] AS la, type(r) AS t, r.amount AS amt,
                   b {{.id, .name}} AS b, labels(b)[0] AS lb LIMIT $limit""",
            {"ids": ids, "limit": limit}, read_only=True)
    nodes, edges = {}, {}
    for r in rows:
        nodes[r["a"]["id"]] = {"id": r["a"]["id"], "name": r["a"]["name"], "type": r["la"]}
        nodes[r["b"]["id"]] = {"id": r["b"]["id"], "name": r["b"]["name"], "type": r["lb"]}
        key = (r["a"]["id"], r["b"]["id"], r["t"])
        e = edges.setdefault(key, {"from": key[0], "to": key[1], "type": r["t"], "count": 0, "amount": 0})
        e["count"] += 1
        e["amount"] += r["amt"] or 0
    for e in edges.values():
        e["label"] = inr(e["amount"]) if e["amount"] else ""
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}

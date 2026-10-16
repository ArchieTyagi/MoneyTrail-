"""
One place that talks to FalkorDB.

`run()` sends a Cypher query and gives back a list of plain dicts,
so the rest of the code never has to deal with the raw client objects.
"""

import os

from dotenv import load_dotenv
from falkordb import FalkorDB
from falkordb.edge import Edge
from falkordb.node import Node
from falkordb.path import Path

load_dotenv()

GRAPH_NAME = os.getenv("GRAPH_NAME", "traceledger")

_graph = None


def get_graph():
    global _graph
    if _graph is None:
        db = FalkorDB(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            username=os.getenv("FALKORDB_USERNAME") or None,
            password=os.getenv("FALKORDB_PASSWORD") or None,
        )
        _graph = db.select_graph(GRAPH_NAME)
    return _graph


def to_plain(value):
    """Turn FalkorDB nodes / edges / paths into dicts and lists."""
    if isinstance(value, Node):
        return {"_label": value.labels[0] if value.labels else None, **value.properties}
    if isinstance(value, Edge):
        return {"_type": value.relation, **value.properties}
    if isinstance(value, Path):
        # map FalkorDB's internal ids to our own "id" property, so edges say who -> who
        ids = {n.id: n.properties.get("id") for n in value.nodes()}
        edges = []
        for e in value.edges():
            edges.append({"_type": e.relation, "from": ids.get(e.src_node),
                          "to": ids.get(e.dest_node), **e.properties})
        return {"nodes": [to_plain(n) for n in value.nodes()], "edges": edges}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return value


def run(query, params=None, read_only=False):
    g = get_graph()
    res = g.ro_query(query, params) if read_only else g.query(query, params)
    cols = [h[1] for h in res.header] if res.header else []
    return [dict(zip(cols, [to_plain(v) for v in row])) for row in res.result_set]

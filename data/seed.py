"""
Loads data/seed.json into FalkorDB. Wipes the graph first, so it's safe to re-run.

Run:  python -m data.seed              (always reload)
      python -m data.seed --if-empty   (only load if the graph is empty, keeps saved cases)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_graph, run  # noqa: E402

SEED = Path(__file__).parent / "seed.json"


def main():
    if not SEED.exists():
        print("seed.json not found, run: python data/generate.py")
        sys.exit(1)
    d = json.loads(SEED.read_text())

    if "--if-empty" in sys.argv:
        try:
            if run("MATCH (n) RETURN count(n) AS n")[0]["n"] > 0:
                print("graph already has data, skipping seed")
                return
        except Exception:
            pass

    g = get_graph()
    try:
        g.delete()
    except Exception:
        pass  # graph did not exist yet

    # Indexes so lookups by id are instant
    for label in ["Person", "Company", "Account", "Address", "Case"]:
        try:
            run(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
        except Exception:
            pass
    run("CREATE INDEX FOR (n:Company) ON (n.name)")
    run("CREATE INDEX FOR (n:Person) ON (n.name)")

    # Nodes
    run("UNWIND $rows AS r CREATE (:Person {id:r.id, name:r.name, pan:r.pan, city:r.city})",
        {"rows": d["persons"]})
    run("""UNWIND $rows AS r CREATE (:Company {id:r.id, name:r.name, gstin:r.gstin, city:r.city,
            incorporated_on:r.incorporated_on, is_client:r.is_client})""", {"rows": d["companies"]})
    run("UNWIND $rows AS r CREATE (:Account {id:r.id, name:r.name, bank:r.bank, number:r.number})",
        {"rows": d["accounts"]})
    run("UNWIND $rows AS r CREATE (:Address {id:r.id, name:r.name, city:r.city})",
        {"rows": d["addresses"]})

    # Edges
    run("""UNWIND $rows AS r MATCH (p:Person {id:r.from}), (c:Company {id:r.to})
           CREATE (p)-[:DIRECTOR_OF {since:r.since}]->(c)""", {"rows": d["director_of"]})
    run("""UNWIND $rows AS r MATCH (a:Person {id:r.from}), (b:Person {id:r.to})
           CREATE (a)-[:RELATIVE_OF {relation:r.relation}]->(b)""", {"rows": d["relative_of"]})
    run("""UNWIND $rows AS r MATCH (c:Company {id:r.from}), (a:Address {id:r.to})
           CREATE (c)-[:REGISTERED_AT]->(a)""", {"rows": d["registered_at"]})
    run("""UNWIND $rows AS r MATCH (h {id:r.from}), (a:Account {id:r.to})
           CREATE (h)-[:HOLDS]->(a)""", {"rows": d["holds"]})
    run("""UNWIND $rows AS r MATCH (a:Account {id:r.from}), (b:Account {id:r.to})
           CREATE (a)-[:TRANSFER {amount:r.amount, date:r.date, ref:r.ref}]->(b)""",
        {"rows": d["transfers"]})
    run("""UNWIND $rows AS r MATCH (s:Company {id:r.from}), (b:Company {id:r.to})
           CREATE (s)-[:INVOICED {invoice_no:r.invoice_no, amount:r.amount, gst:r.gst, date:r.date}]->(b)""",
        {"rows": d["invoices"]})

    counts = run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label")
    rels = run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY type")
    print("Loaded graph:")
    for c in counts:
        print(f"  {c['label']:10s} {c['n']}")
    for r in rels:
        print(f"  {r['type']:13s} {r['n']}")


if __name__ == "__main__":
    main()

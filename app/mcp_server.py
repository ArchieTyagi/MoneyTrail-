"""
MCP server: exposes the graph functions in app/queries.py as tools.

Any MCP client can use it: our own agent (app/agent.py) starts it over stdio,
and you can also plug it into Claude Desktop, Cursor, Windsurf etc.

Run by itself:  python -m app.mcp_server
"""

from mcp.server.fastmcp import FastMCP

from app import queries as q

mcp = FastMCP("traceledger-graph", log_level="WARNING")


# ---------------- look around

@mcp.tool()
def search_entity(query: str) -> dict:
    """Find a company, person, bank account or address by name. Use this first to get the id
    of whatever the user mentions. Returns up to 10 matches with type, id and name."""
    return q.search_entity(query)


@mcp.tool()
def entity_profile(entity_id: str) -> dict:
    """Everything one hop around an entity: directors, address, accounts, other companies at the
    same address, top buyers/sellers (invoices), top payers/payees (money), relatives (for people),
    and how many cases the firm already opened on it."""
    return q.entity_profile(entity_id)


@mcp.tool()
def scan_portfolio() -> dict:
    """Quick scan of the whole client graph for leads: invoice loops, company groups that trade
    heavily among themselves, accounts money flows through, addresses shared by many companies.
    Good starting point when no specific company is named."""
    return q.scan_portfolio()


# ---------------- multi-hop pattern checks

@mcp.tool()
def find_invoice_cycles(company_id: str, max_hops: int = 6) -> dict:
    """GST circular trading check. Finds chains of invoices that start at this company, pass
    through other companies and come back to it (A invoices B, B invoices C ... back to A).
    Real trade rarely loops; loops usually mean fake invoices to inflate turnover or claim input credit."""
    return q.find_invoice_cycles(company_id, max_hops)


@mcp.tool()
def find_money_cycles(entity_id: str, max_hops: int = 5) -> dict:
    """Money that leaves this company/person/account and returns to the SAME account through
    other accounts, in time order."""
    return q.find_money_cycles(entity_id, max_hops)


@mcp.tool()
def find_round_tripping(company_id: str, max_hops: int = 4) -> dict:
    """Money that leaves the company through a third party and lands in the personal account of
    one of its own directors or their relatives. Ignores direct salary. Strong sign of siphoning."""
    return q.find_round_tripping(company_id, max_hops)


@mcp.tool()
def find_related_counterparties(company_id: str) -> dict:
    """Checks every company this one trades with (money or invoices) and reports the ones that are
    secretly connected to it through directors, family or a shared address, with the link path.
    These are undisclosed related-party transactions."""
    return q.find_related_counterparties(company_id)


@mcp.tool()
def explain_link(entity_a: str, entity_b: str, max_hops: int = 5) -> dict:
    """Shortest ownership / family / address connection between two entities (ignores money).
    Use it to prove two parties are related."""
    return q.explain_link(entity_a, entity_b, max_hops)


@mcp.tool()
def trace_money(from_id: str, to_id: str, max_hops: int = 4) -> dict:
    """Follow money from one party to another through middle accounts, only along routes that move
    forward in time. Several similar parallel routes = a payment was split up to hide it (layering)."""
    return q.trace_money(from_id, to_id, max_hops)


# ---------------- graph algorithms

@mcp.tool()
def find_hub_accounts(top_k: int = 5) -> dict:
    """Runs betweenness centrality on the money graph to find accounts that lots of money passes
    THROUGH, with number of payers and pass-through ratio. New entities with high scores are
    possible collection/mule accounts."""
    return q.find_hub_accounts(top_k)


@mcp.tool()
def find_control_groups(min_companies: int = 2) -> dict:
    """Runs connected components on director/family/address links to find groups of companies under
    common control, ranked by how much they invoice and pay each other."""
    return q.find_control_groups(min_companies)


@mcp.tool()
def find_shell_clusters(min_companies: int = 3) -> dict:
    """Addresses where several companies are registered, with their incorporation dates."""
    return q.find_shell_clusters(min_companies)


# ---------------- memory + actions

@mcp.tool()
def get_prior_cases(entity_id: str) -> dict:
    """Cases the firm opened on this entity in earlier sessions (stored in the graph)."""
    return q.get_prior_cases(entity_id)


@mcp.tool()
def open_case(title: str, risk_score: int, summary: str, entity_ids: list[str],
              evidence: list[str], client_query_note: str, recommended_action: str) -> dict:
    """ACTION: record a finding in the graph as a Case linked to every entity involved.
    risk_score 0-100. evidence = the exact path texts returned by other tools (copy them, don't invent).
    client_query_note = short polite note the CA can send the client asking them to explain the
    transactions. recommended_action = what the auditor should do next."""
    return q.open_case(title, risk_score, summary, entity_ids, evidence, client_query_note, recommended_action)


@mcp.tool()
def escalate_case(case_id: str, reason: str) -> dict:
    """ACTION: escalate a case to the partner. Use for risk_score 75 or more."""
    return q.escalate_case(case_id, reason)


if __name__ == "__main__":
    mcp.run()

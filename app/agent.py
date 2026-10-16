"""
The agent. It does NOT know the data. It only knows the tools the MCP server offers,
and it decides which tool to call next based on what the last tool returned.

Loop:
  task -> LLM picks a tool -> MCP server runs Cypher on FalkorDB -> result back to LLM -> repeat
  ... until it has proof, writes a Case into the graph, and gives a final answer.

Works with any OpenAI-compatible API (OpenAI, Groq, OpenRouter, Gemini, local Ollama).

CLI:  python -m app.agent "Investigate Sharma Traders"
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from openai import AsyncOpenAI

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "16"))

SYSTEM_PROMPT = """You are a forensic audit assistant inside a Chartered Accountant (CA) firm in India.
The firm's client data is stored as a graph: companies, people (directors), their relatives,
registered addresses, bank accounts, money transfers and GST invoices. You can only see it through tools.

Your job: investigate what the user asks, find out whether something is wrong, prove it, and act.

How to work:
1. Find the entity ids first (search_entity), then look around (entity_profile).
   If no company is named, start with scan_portfolio.
2. Always call get_prior_cases on the main entity, so you know what the firm found before.
3. Follow leads. Pick the next tool based on what the last result showed. Typical patterns:
   - a big vendor with few invoices, or a new vendor -> find_related_counterparties, explain_link
   - money going to someone close to the company -> find_round_tripping, trace_money
   - companies at the same address / loops in invoices -> find_invoice_cycles, find_shell_clusters
   - one payment split across many middle accounts -> trace_money (look at looks_like_split_payment)
   - an account many unrelated firms pay into -> find_hub_accounts, entity_profile on its holder
4. Never claim anything you did not see in a tool result. Every finding needs a path from a tool.
5. When you have proof, call open_case once per distinct problem:
   - risk_score: 0-100 (75+ = serious, e.g. undisclosed related party with money coming back, fake invoice ring)
   - evidence: copy the exact path text from tool results
   - entity_ids: every company, person and account in the evidence
   - client_query_note: a short, polite note (3-5 lines) the CA can send to the client asking them to
     explain these transactions and share supporting documents. Don't accuse.
   - recommended_action: what the auditor should do next (e.g. check related-party disclosure under
     Section 188 / AS 18, verify GST input credit, ask for loan agreements, report if needed)
   If risk_score >= 75, also call escalate_case.
   If nothing is wrong, say so and don't open a case.
6. Finish with a short answer in plain English:
   - Verdict (clean / suspicious / high risk)
   - What you found, each point with its path (A -> B -> C)
   - Case ids you opened and what happens next
Keep amounts in the ₹L / ₹Cr form the tools give you."""


def _llm():
    return AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL") or None,
        api_key=os.getenv("LLM_API_KEY") or "not-needed",
    )


def _collect_ids(obj, out):
    """Pull every node id the tool result points at, so the UI can light up the path."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("node_ids", "company_ids", "entity_ids") and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str))
            elif k in ("id", "holder_id", "account_id", "case_id") and isinstance(v, str):
                out.add(v)
            else:
                _collect_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ids(v, out)
    return out


async def investigate(task: str):
    """Async generator. Yields events the UI shows live:
    start, thought, tool_call, tool_result, final, error."""
    llm = _llm()
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    server = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                   cwd=str(ROOT), env=dict(os.environ))

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            tool_specs = [{"type": "function",
                           "function": {"name": t.name, "description": t.description,
                                        "parameters": t.inputSchema}} for t in tools]
            yield {"type": "start", "task": task, "tools": [t.name for t in tools]}

            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": task}]

            for step in range(MAX_STEPS):
                last_round = step == MAX_STEPS - 1
                if last_round:
                    messages.append({"role": "user", "content": "Step limit reached. Give your final answer now."})
                try:
                    resp = await llm.chat.completions.create(
                        model=model, messages=messages, temperature=0.1,
                        tools=None if last_round else tool_specs)
                except Exception as e:
                    yield {"type": "error", "text": f"LLM call failed: {e}"}
                    return
                msg = resp.choices[0].message
                calls = msg.tool_calls or []

                messages.append({
                    "role": "assistant", "content": msg.content or "",
                    **({"tool_calls": [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name,
                                                     "arguments": c.function.arguments or "{}"}}
                                       for c in calls]} if calls else {}),
                })

                if not calls:
                    yield {"type": "final", "text": msg.content or "(no answer)"}
                    return

                if msg.content:
                    yield {"type": "thought", "text": msg.content}

                for c in calls:
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    yield {"type": "tool_call", "step": step + 1, "name": c.function.name, "args": args}

                    try:
                        res = await session.call_tool(c.function.name, args)
                        text = "".join(getattr(part, "text", "") for part in res.content)
                    except Exception as e:
                        text = json.dumps({"error": str(e)})
                    try:
                        data = json.loads(text)
                    except ValueError:
                        data = {"raw": text}

                    ids = _collect_ids(data, set())
                    if c.function.name == "open_case":
                        ids |= set(args.get("entity_ids", []))
                    yield {"type": "tool_result", "name": c.function.name, "result": data,
                           "highlight": sorted(ids)}

                    messages.append({"role": "tool", "tool_call_id": c.id, "content": text[:8000]})


async def _cli(task):
    async for ev in investigate(task):
        t = ev["type"]
        if t == "tool_call":
            print(f"\n[{ev['step']}] -> {ev['name']}({json.dumps(ev['args'], ensure_ascii=False)})")
        elif t == "tool_result":
            print("    " + json.dumps(ev["result"], ensure_ascii=False)[:400])
        elif t in ("thought", "final", "error"):
            print(f"\n{t.upper()}: {ev['text']}")
        elif t == "start":
            print(f"tools: {', '.join(ev['tools'])}")


if __name__ == "__main__":
    asyncio.run(_cli(" ".join(sys.argv[1:]) or "Scan the portfolio and investigate the biggest risk."))

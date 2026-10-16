"""
Web backend.

  GET  /                     the UI
  POST /api/investigate      run the agent, streams each step as Server-Sent Events
  GET  /api/graph?ids=a,b    nodes around these ids (for drawing)
  GET  /api/cases            cases the agent has written into the graph
  GET  /api/health           is FalkorDB reachable?

Run:  uvicorn app.api:app --reload
"""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import queries as q
from app.agent import investigate
from app.db import run

app = FastAPI(title="TraceLedger")
STATIC = Path(__file__).parent / "static"


class Task(BaseModel):
    task: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    try:
        n = run("MATCH (n) RETURN count(n) AS n", read_only=True)[0]["n"]
        return {"ok": True, "nodes": n}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/graph")
def graph(ids: str = "", hops: int = 1):
    id_list = [i for i in ids.split(",") if i]
    return q.subgraph(id_list, hops=hops, limit=1500)


@app.get("/api/cases")
def cases():
    return q.list_cases()


@app.post("/api/investigate")
async def run_investigation(body: Task):
    async def stream():
        try:
            async for event in investigate(body.task):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

"""
No Docker? This starts a real FalkorDB server on localhost:6379 using the
falkordblite package (it ships the FalkorDB engine inside a pip package).

Run:  python scripts/local_falkordb.py      (keep this terminal open)

Docker is still the recommended way:  docker compose up falkordb
"""

import os
import time
from pathlib import Path

from redislite.falkordb_client import FalkorDB

port = os.getenv("FALKORDB_PORT", "6379")
db_file = Path(__file__).resolve().parent.parent / ".falkordb" / "local.db"
db_file.parent.mkdir(exist_ok=True)

db = FalkorDB(str(db_file), serverconfig={"port": port})
print(f"FalkorDB running on localhost:{port}  (data file: {db_file})")
print("Press Ctrl+C to stop.")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("stopped")

#!/usr/bin/env python3
"""Concurrency test: do parallel writers lose entries from the beacon store?

Spawns N OS processes (matching `gunicorn --workers 2` multi-process reality;
threads would be masked by the GIL) that each append one crawl record to a
shared store file, then checks how many actually survived.
"""
import json
import multiprocessing
import os
import sys
import tempfile
from multiprocessing import Process

# 'spawn' re-imports this module in every child, which would re-run mkdtemp and
# give each writer its own private store -- hiding the very race under test.
multiprocessing.set_start_method("fork", force=True)

N = 40
store_path = os.environ.get("RACE_STORE") or os.path.join(tempfile.mkdtemp(), "store.json")
os.environ["BEACON_STORE"] = store_path
os.environ.setdefault("SML_API_KEY", "test")

MODE = sys.argv[1] if len(sys.argv) > 1 else "fixed"


def writer(i):
    import beacon
    if MODE == "old":
        # Reproduce the pre-fix path: load and save as separate lock scopes.
        store = beacon._load_store()
        crawl = store.setdefault("crawl", [])
        crawl.append({"ts": i, "url": f"u{i}", "ok": True, "note": ""})
        store["crawl"] = crawl[-500:]
        beacon._save_store(store)
    else:
        beacon.record_crawl(f"u{i}", True, "")


if __name__ == "__main__":
    ps = [Process(target=writer, args=(i,)) for i in range(N)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()

    data = json.load(open(store_path))
    got = len(data.get("crawl", []))
    urls = {c["url"] for c in data.get("crawl", [])}
    print(f"  mode={MODE}  writers={N}  survived={got}  unique={len(urls)}  LOST={N - len(urls)}")
    sys.exit(0 if len(urls) == N else 1)

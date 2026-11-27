#!/usr/bin/env python3
"""Concurrency load test for a running PyBoxD server.

Fires N simultaneous /run requests (each one compiles or runs real code) and
reports how many were judged, queued past the timeout (429), or failed.

    PYBOXD_AUTH_TOKEN=... python scripts/load_test.py --url http://localhost:8080 \
        --concurrency 10 25 50 75 100 --language py3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from collections import Counter

import httpx

JOBS = {
    "py3": "a, b = map(int, input().split())\nprint(a + b)\n",
    "cpp": (
        "#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b<<'\\n';}\n"
    ),
    "c": '#include <stdio.h>\nint main(){int a,b;scanf("%d %d",&a,&b);printf("%d\\n",a+b);}\n',
}


async def one(client: httpx.AsyncClient, language: str) -> tuple[str, float]:
    job = {
        "language": language,
        "source": JOBS[language],
        "tests": [{"stdin": "20 22", "expected_stdout": "42\n"}],
    }
    start = time.perf_counter()
    try:
        r = await client.post("/run", json=job)
    except httpx.HTTPError as exc:
        return f"error:{type(exc).__name__}", time.perf_counter() - start
    elapsed = time.perf_counter() - start
    if r.status_code == 200:
        return r.json()["status"], elapsed
    return f"http_{r.status_code}", elapsed


async def level(url: str, token: str, n: int, language: str) -> None:
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=url, headers=headers, limits=limits, timeout=120
    ) as client:
        start = time.perf_counter()
        results = await asyncio.gather(*(one(client, language) for _ in range(n)))
        wall = time.perf_counter() - start
    counts = Counter(status for status, _ in results)
    lat = sorted(t for _, t in results)
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
    print(
        f"c={n:<4} wall={wall:6.2f}s  rps={n / wall:6.1f}  "
        f"p50={statistics.median(lat):5.2f}s p95={p95:5.2f}s  {dict(counts)}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--token", default=os.environ.get("PYBOXD_AUTH_TOKEN", ""))
    p.add_argument("--language", default="py3", choices=sorted(JOBS))
    p.add_argument("--concurrency", type=int, nargs="+", default=[10, 25, 50, 75, 100])
    args = p.parse_args()
    if not args.token:
        p.error("set PYBOXD_AUTH_TOKEN or pass --token")
    for n in args.concurrency:
        asyncio.run(level(args.url, args.token, n, args.language))


if __name__ == "__main__":
    main()

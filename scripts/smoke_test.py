#!/usr/bin/env python3
"""Smoke-test a running PyBoxD server: auth, readiness, and one job per language.

PYBOXD_AUTH_TOKEN=... python scripts/smoke_test.py --url http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Each program reads an integer and prints it doubled.
PROGRAMS = {
    "py3": "print(int(input()) * 2)",
    "c": '#include <stdio.h>\nint main(){int x; scanf("%d",&x); printf("%d\\n",x*2);}',
    "cpp": "#include <iostream>\nint main(){int x; std::cin>>x; std::cout<<x*2<<'\\n';}",
    "java": (
        "import java.util.*;\npublic class Solution { public static void main(String[] a)"
        " { System.out.println(new Scanner(System.in).nextInt() * 2); } }"
    ),
    "javascript": 'const x = require("fs").readFileSync(0, "utf8"); console.log(Number(x) * 2);',
    "bash": "read x; echo $((x * 2))",
    "rust": (
        "use std::io::*;\nfn main(){ let mut s = String::new(); stdin().read_line(&mut s)"
        '.unwrap(); println!("{}", s.trim().parse::<i64>().unwrap() * 2); }'
    ),
    "verilog": (
        "module m; integer x; initial begin"
        ' if ($fscanf(32\'h8000_0000, "%d", x)) $display("%0d", x * 2); $finish; end endmodule'
    ),
}


def call(url: str, token: str | None, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(url + path, method="POST" if body else "GET")  # noqa: S310
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=120) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--token", default=os.environ.get("PYBOXD_AUTH_TOKEN", ""))
    p.add_argument("--languages", nargs="*", help="default: every language the server reports")
    p.add_argument("--wait", type=int, default=30, help="seconds to wait for /healthz")
    args = p.parse_args()

    for _ in range(args.wait):
        try:
            if call(args.url, None, "/healthz")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        print("server did not become healthy")
        return 1

    failures = 0
    code, _ = call(args.url, None, "/info")
    print(f"/info without token -> {code} (want 401)")
    failures += code != 401

    code, ready = call(args.url, args.token, "/readyz")
    print(f"/readyz -> {code} {ready.get('status')} sandbox={ready.get('sandbox_mode')}")
    languages = args.languages or sorted(ready.get("languages", {}))

    for lang in languages:
        if lang not in PROGRAMS:
            print(f"{lang:11} skipped (no smoke program)")
            continue
        job = {
            "language": lang,
            "source": PROGRAMS[lang],
            "tests": [{"stdin": "21\n", "expected_stdout": "42\n"}],
        }
        code, body = call(args.url, args.token, "/run", job)
        status = body.get("status") or body.get("error", {}).get("code")
        ok = code == 200 and status == "accepted"
        failures += not ok
        detail = ""
        if not ok:
            detail = json.dumps(body)[:400]
        print(f"{lang:11} {'PASS' if ok else 'FAIL'} {status} {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

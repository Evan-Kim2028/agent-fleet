#!/usr/bin/env python3
"""lean_gate_claims.py CANDIDATES_JSONL LSLUG — one line per claim for fbgate's batched verifier prompt."""

import json
import re
import sys

for line in open(sys.argv[1]):
    if line.strip():
        c = json.loads(line)
        i = re.sub(r"\W", "_", c.get("id", "x"))[:30]
        print(
            f"- id={i} test_file_name=test_gate_{sys.argv[2]}_{i}.py claim={json.dumps(c)}"
        )

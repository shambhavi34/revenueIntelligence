"""python -m evals.run [--provider mock|replay|anthropic|auto]   -> pass/fail table, non-zero exit on failure."""
from __future__ import annotations

import argparse
import sys

from revintel import pipeline

from .cases import CASES


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="mock", choices=["auto", "anthropic", "openai", "replay", "mock"])
    a = ap.parse_args(argv)
    res = pipeline.run(a.provider)
    fails = 0
    for c in CASES:
        try:
            c.fn(res)
            print(f"PASS  {c.id:<36} {c.why}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {c.id:<36} {e}")
    print(f"\n{len(CASES) - fails}/{len(CASES)} passed (provider: {res['briefing']['providers']})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

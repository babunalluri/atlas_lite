#!/usr/bin/env python3
"""Train the AI agent on option-tape minutes and write its model file.

The book stays a separate paper ledger. This script does not place orders and
does not change the scorecard agent.

  python3 scripts/train_ai_agent.py --dir data/recordings --out atlas_lite/ai_agent_model.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas_lite.ai_agent_model import save_model, walk_forward  # noqa: E402
from scripts.score_recordings import load_minute_rows  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward train the AI agent model")
    parser.add_argument("--dir", type=Path, default=ROOT / "data" / "recordings")
    parser.add_argument("--out", type=Path, default=ROOT / "atlas_lite" / "ai_agent_model.json")
    args = parser.parse_args()
    if not args.dir.is_dir():
        print(f"missing recordings dir: {args.dir}", file=sys.stderr)
        return 1
    rows = load_minute_rows(args.dir)
    report = walk_forward(rows)
    save_model(args.out, report)
    summary = {
        "promoted": report.get("promoted"),
        "reason": report.get("reason"),
        "train_days": len(report.get("train_days") or []),
        "test_days": report.get("test_days"),
        "test_net": report.get("test_net"),
        "test_trades": report.get("test_trades"),
        "scorecard_test_net": report.get("scorecard_test_net"),
        "scorecard_test_trades": report.get("scorecard_test_trades"),
        "train_samples": report.get("train_samples"),
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

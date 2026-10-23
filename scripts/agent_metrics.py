"""业务价值指标 CLI（V20 亮点 4）。

用法：
    PYTHONPATH=backend python scripts/agent_metrics.py                # 读全部 trace 打印报告
    PYTHONPATH=backend python scripts/agent_metrics.py --out docs/reports/agent-metrics-latest.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

    parser = argparse.ArgumentParser(description="Agent 业务价值指标（从 agent_traces JSONL）")
    parser.add_argument("--out", help="Markdown 报告输出路径（默认仅打印）")
    args = parser.parse_args()

    from app.core.config import settings
    from app.services.chat.agent_metrics import collect_trace_metrics, render_markdown

    trace_dir = Path(getattr(settings, "AGENT_TRACE_DIR", "data/agent_traces"))
    rows: list[dict] = []
    if trace_dir.is_dir():
        for f in sorted(trace_dir.glob("trace-*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 坏行跳过，不让一条脏数据毁掉整个报告

    metrics = collect_trace_metrics(rows)
    report = render_markdown(metrics)

    print(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"[saved] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

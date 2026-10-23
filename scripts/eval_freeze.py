"""黄金评测集冻结/校验 CLI（V21-EV 治理）。

用法（仓库根目录）：
    PYTHONPATH=backend python scripts/eval_freeze.py --freeze --version v1.0   # 发版冻结
    PYTHONPATH=backend python scripts/eval_freeze.py --verify                  # 校验（CI / 评测前）
    PYTHONPATH=backend python scripts/eval_freeze.py --verify --files data/eval_multiturn_v1.json

纪律：--freeze 是发版动作——每次使用必须在 data/evalsets/CHANGELOG.md 记录
"为什么增删改"。评测脚本在跑之前必须先 --verify，失败即拒绝评测。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="黄金评测集冻结 / 防篡改校验")
    parser.add_argument("--freeze", action="store_true", help="生成/更新 MANIFEST（发版动作）")
    parser.add_argument("--verify", action="store_true", help="校验当前评测集与 MANIFEST 一致")
    parser.add_argument("--version", default="v1.0", help="freeze 时的版本号")
    parser.add_argument("--files", nargs="*", help="只处理指定评测集文件（默认全部受管文件）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    # golden_manifest 纯标准库：backend 与仓库根都加进 path（两种部署形态）
    sys.path.insert(0, str(root / "backend"))
    sys.path.insert(0, str(root))

    from app.services.eval.golden_manifest import freeze, verify_manifest

    if args.freeze:
        manifest = freeze(root, args.files, args.version)
        total = len(manifest["cases_sha256"])
        print(f"[frozen] version={manifest['version']} files={len(manifest['files'])} cases={total}")
        print("[warn] 发版纪律：请在 data/evalsets/CHANGELOG.md 记录本次增删改的原因与审批人")
        return 0

    if args.verify:
        ok, violations = verify_manifest(root, args.files)
        if ok:
            print("[verify] PASS —— 评测集与 MANIFEST 一致")
            return 0
        print(f"[verify] FAIL —— {len(violations)} 处不一致，评测结果不可信：")
        for v in violations:
            print(f"  - {v}")
        print("[fix] 走发版流程：eval_freeze.py --freeze --version vX.Y + CHANGELOG 记录")
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

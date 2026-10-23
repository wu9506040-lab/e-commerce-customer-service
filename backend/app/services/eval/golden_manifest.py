"""黄金评测集冻结与防篡改（V21-EV 治理 · MANIFEST 机制）。

背景（踩过的坑）：
- 60 题话术评测曾发生过"3 处 expected 对齐答案措辞"——为了分数好看改评测集，
  评测就失去了公信力。本模块把这类改动变成**机制上不可能静默发生**：
  每题 query+expected 的规范化 JSON 做 sha256，全部记入 MANIFEST；
  评测跑前校验，任何改动 → hash 不符 → 拒绝跑（除非走"发版"流程重新 freeze
  并在 CHANGELOG 记录）。

使用契约：
- `scripts/eval_freeze.py --freeze`  : 生成/更新 MANIFEST（发版权限，慎用）
- `scripts/eval_freeze.py --verify`  : 校验（CI / 评测入口强制先跑）
- 库调用：verify_manifest(files) -> (ok, violations)；评测脚本在 main 里先验。

MANIFEST 结构（data/evalsets/MANIFEST.json）：
{
  "version": "v1.0",
  "frozen_at": <epoch_ms>,
  "files": {
      "data/eval_set_v1.json":     {"sha256": "...", "cases": 330},
      "data/eval_multiturn_v1.json": {"sha256": "...", "cases": 20}
  },
  "cases_sha256": {
      "data/eval_set_v1.json#0":   "a3f...",
      "data/eval_multiturn_v1.json#MT01": "b7e...",
      ...
  }
}
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MANIFEST_PATH = "data/evalsets/MANIFEST.json"
DEFAULT_MANAGED_FILES = [
    "data/eval_set_v1.json",
    "data/eval_multiturn_v1.json",
]

_ID_FIELDS = ("scenario_id", "id", "query")  # case 身份字段（优先级从高到低）


def _canonical(obj: Any) -> str:
    """规范化 JSON（键排序、去空白差异），保证哈希只反映内容变化。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _case_id(path: str, idx: int, case: dict) -> str:
    for f in _ID_FIELDS:
        if f in case:
            return f"{path}#{case[f]}"
    return f"{path}#{idx}"


def build_manifest(root: Path, files: Optional[list[str]] = None,
                   version: str = "v1.0") -> dict:
    """扫描受管评测集，生成 MANIFEST dict（不落盘）。"""
    managed = files or DEFAULT_MANAGED_FILES
    manifest: dict[str, Any] = {
        "version": version,
        "frozen_at": int(time.time() * 1000),
        "files": {},
        "cases_sha256": {},
    }
    for rel in managed:
        p = root / rel
        if not p.is_file():
            logger.warning("[manifest] 受管文件缺失，跳过: %s", rel)
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        manifest["files"][rel] = {
            "sha256": _sha256(_canonical(data)),
            "cases": len(data),
        }
        for i, case in enumerate(data):
            manifest["cases_sha256"][_case_id(rel, i, case)] = _sha256(_canonical(case))
    return manifest


def verify_manifest(root: Path, files: Optional[list[str]] = None) -> tuple[bool, list[str]]:
    """校验当前文件是否与 MANIFEST 一致。

    Returns:
        (True, [])       —— 全部一致
        (False, 残差列表) —— 文件级/题目级不一致明细；或 MANIFEST 缺失
    """
    mp = root / MANIFEST_PATH
    if not mp.is_file():
        return False, [f"MANIFEST 不存在: {MANIFEST_PATH}（先跑 eval_freeze.py --freeze）"]

    manifest = json.loads(mp.read_text(encoding="utf-8"))
    violations: list[str] = []
    # files 未指定 → 校验 MANIFEST 管理的全部文件（而非 DEFAULT 列表——
    # 否则新冻结的评测集会被静默跳过，篡改检不出）
    managed = files or list(manifest.get("files", {}).keys())

    for rel in managed:
        p = root / rel
        recorded = manifest.get("files", {}).get(rel)
        if recorded is None:
            if p.is_file():
                violations.append(f"{rel}: 文件存在但 MANIFEST 未管理（疑似新增未冻结）")
            continue
        if not p.is_file():
            violations.append(f"{rel}: 受管文件被删除")
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        file_hash = _sha256(_canonical(data))
        if file_hash != recorded.get("sha256"):
            # 文件级不符 → 逐题定位（可读性：告诉用户改了哪道题）
            old_cases = manifest.get("cases_sha256", {})
            known_ids = {k for k in old_cases if k.startswith(rel + "#")}
            for i, case in enumerate(data):
                cid = _case_id(rel, i, case)
                if cid not in known_ids:
                    violations.append(f"{cid}: 新增题（未发版冻结）")
                    continue
                known_ids.discard(cid)  # 存在于当前文件 → 不算 stale（先移除再查改）
                if old_cases[cid] != _sha256(_canonical(case)):
                    violations.append(f"{cid}: 内容被修改（expected/query 变更未走发版流程）")
            for stale in known_ids:
                if stale.startswith(rel + "#"):
                    violations.append(f"{stale}: 题被删除（未走发版流程）")

    return (len(violations) == 0), violations


def freeze(root: Path, files: Optional[list[str]] = None,
           version: str = "v1.0") -> dict:
    """生成并落盘 MANIFEST（发版动作——调用方负责 CHANGELOG）。"""
    manifest = build_manifest(root, files, version)
    mp = root / MANIFEST_PATH
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[manifest] 冻结完成 version=%s files=%s cases=%d",
                version, list(manifest["files"]), len(manifest["cases_sha256"]))
    return manifest

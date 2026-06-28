#!/usr/bin/env python3
"""
爬商品封面图（10 张 SKU001-SKU010）到 frontend/public/products/

策略（多源 fallback，永不阻塞）：
1. 主源：Unsplash Source（已 deprecated 但通常仍工作）— 按品类关键词
2. 备源：dummyimage.com — 永远 200，返回品类占位图（带 SKU 文字）
3. 兜底：本地生成 SVG（品类渐变 + emoji）— 离线可用

版权说明：
- Unsplash：Unsplash License（可商用，无需署名）
- dummyimage：CC0
- SVG 兜底：本项目自有

依赖：requests（pip install requests）
"""
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

# =============================================================
# SKU → 品类映射
# 来自 scripts/seed_ecommerce_data.py 的真实 SKU
# =============================================================
# (sku,    name_for_image,   category_keyword,  hex_bg,  hex_fg,  emoji)
SKUS = [
    ("SKU001", "ZP1 Phone",        "phone",      "667eea", "ffffff", "📱"),
    ("SKU002", "ZP2Pro Phone",     "phone",      "f5576c", "ffffff", "📱"),
    ("SKU003", "ZN1 Phone",        "phone",      "00f2fe", "333333", "📱"),
    ("SKU004", "ZN2 Phone",        "phone",      "38f9d7", "333333", "📱"),
    ("SKU005", "BP1 Earphone",     "earphone",   "fee140", "333333", "🎧"),
    ("SKU006", "WS1 Watch",        "watch",      "330867", "ffffff", "⌚"),
    ("SKU007", "PT1 Tablet",       "tablet",     "fed6e3", "333333", "📲"),
    ("SKU008", "LB1 Laptop",       "laptop",     "b490ca", "ffffff", "💻"),
    ("SKU009", "KB1 Keyboard",     "keyboard",   "fa71cd", "ffffff", "⌨️"),
    ("SKU010", "MS1 Mouse",        "mouse",      "80d0c7", "333333", "🖱️"),
]

OUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "products"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================
# 1. Unsplash Source（首选：真实商品/场景图）
# =============================================================
def fetch_unsplash(sku: str, keyword: str) -> bytes | None:
    """从 source.unsplash.com 拉一张图（600x600）"""
    url = f"https://source.unsplash.com/600x600/?{keyword}"
    try:
        r = requests.get(url, allow_redirects=True, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 5000:
            return r.content
    except Exception as e:
        print(f"  [unsplash {sku}] 失败: {type(e).__name__}")
    return None


# =============================================================
# 2. dummyimage.com（备选：永远 200，品类占位图）
# =============================================================
def fetch_dummyimage(sku: str, name: str, hex_bg: str, hex_fg: str) -> bytes | None:
    """dummyimage.com：600x600 PNG，背景 hex_bg，文字 SKU + name"""
    text = f"{sku}\\n{name}"
    url = f"https://dummyimage.com/600x600/{hex_bg}/{hex_fg}&text={quote(text)}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except Exception as e:
        print(f"  [dummyimage {sku}] 失败: {type(e).__name__}")
    return None


# =============================================================
# 3. SVG 兜底（永远成功）
# =============================================================
def make_svg(sku: str, name: str, emoji: str, hex_bg: str, hex_fg: str) -> bytes:
    """生成 600x600 SVG：纯色背景 + 大字号 SKU + emoji"""
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600">
  <rect width="600" height="600" fill="#{hex_bg}"/>
  <text x="300" y="220" text-anchor="middle" font-size="180">{emoji}</text>
  <text x="300" y="400" text-anchor="middle" font-size="64"
        font-family="sans-serif" font-weight="700" fill="#{hex_fg}">{sku}</text>
  <text x="300" y="450" text-anchor="middle" font-size="28"
        font-family="sans-serif" fill="#{hex_fg}" opacity="0.85">{name}</text>
  <text x="300" y="540" text-anchor="middle" font-size="20"
        font-family="sans-serif" fill="#{hex_fg}" opacity="0.7">智选电商 · 智能客服演示</text>
</svg>'''
    return svg.encode("utf-8")


# =============================================================
# 主流程
# =============================================================
def main():
    print(f"输出目录: {OUT_DIR}")
    print(f"目标: {len(SKUS)} 张图\n")

    stats = {"unsplash": 0, "dummyimage": 0, "svg": 0}

    for sku, name, keyword, hex_bg, hex_fg, emoji in SKUS:
        out_path = OUT_DIR / f"{sku}.jpg"

        # 1) 试 Unsplash（真实图）
        data = fetch_unsplash(sku, keyword)
        if data:
            out_path.write_bytes(data)
            print(f"  ✓ {sku} ← unsplash ({len(data)//1024} KB)")
            stats["unsplash"] += 1
            time.sleep(0.5)
            continue

        # 2) 试 dummyimage（品类占位）
        data = fetch_dummyimage(sku, name, hex_bg, hex_fg)
        if data:
            out_path.write_bytes(data)
            print(f"  ~ {sku} ← dummyimage ({len(data)//1024} KB)")
            stats["dummyimage"] += 1
            time.sleep(0.2)
            continue

        # 3) SVG 兜底（如果上面 jpg 失败，改存 svg）
        svg = make_svg(sku, name, emoji, hex_bg, hex_fg)
        svg_path = OUT_DIR / f"{sku}.svg"
        svg_path.write_bytes(svg)
        print(f"  ! {sku} ← svg fallback ({len(svg)//1024} KB)")
        stats["svg"] += 1

    print(f"\n结果: unsplash={stats['unsplash']} dummyimage={stats['dummyimage']} svg={stats['svg']}")
    print(f"目录: {OUT_DIR}")
    # 至少 5 张真图（unsplash）算 OK；其他都至少有占位
    return 0


if __name__ == "__main__":
    sys.exit(main())

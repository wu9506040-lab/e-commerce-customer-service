"""
xlsx 话术数据 → RAG 知识库 JSON 灌入脚本

源文件：
1. E:\\BaiduNetdiskDownload\\售后话术FAQ及处理逻辑.xlsx （50 条 7 主类）
2. E:\\BaiduNetdiskDownload\\话术\\4.客服培训-精品\\【客服培训】双11活动客服话术.xlsx （11 套）
3. E:\\BaiduNetdiskDownload\\话术\\4.客服培训-精品\\【客服培训】客服售后常用快捷短语汇总.xlsx （41 条）

输出：docs/ecommerce_kb/ 下 8 个 JSON（与现有 faq_top20.json 格式对齐）
设计原则：
- 最低修改：纯新增文件，不动现有 KB
- source ID 用 xlsx_* 前缀，避免与现有 source 重名
- text 格式 Q + A + 处理逻辑 Markdown 拼接，让 RAG 检索命中率高

中文路径处理：openpyxl 在 Windows 上 zipfile 走 ANSI codepage，需要
chdir 到 BaiduNetdiskDownload + 用 entry.path 拿到精确字节路径。
"""
import os
import sys
import json
import openpyxl
from pathlib import Path

SRC_DIR = r'E:\BaiduNetdiskDownload'
XLSX_REFUND_FAQ = os.path.join(SRC_DIR, '售后话术FAQ及处理逻辑.xlsx')
XLSX_COMMON_QA = os.path.join(SRC_DIR, r'话术\4.客服培训-精品\【客服培训】客服常见问题及应对话术.xlsx')
KB_OUT_DIR = r'E:\智能客服\docs\ecommerce_kb'


def resolve_xlsx_path(keyword):
    """在话术目录下找含 keyword 的 xlsx，返回 openpyxl 能读的路径。
    通过 chdir + os.scandir 拿精确文件名（避免 GBK/UTF-8 编码陷阱）。"""
    os.chdir(SRC_DIR)
    # 找 话术/4.客服培训-精品
    huashu = None
    for e in os.scandir('.'):
        if e.is_dir() and e.name == '话术':
            huashu = e.path
            break
    if not huashu:
        raise FileNotFoundError('话术 目录未找到')
    jingpin = None
    for e in os.scandir(huashu):
        if e.is_dir() and '精品' in e.name:
            jingpin = e.path
            break
    if not jingpin:
        raise FileNotFoundError('4.客服培训-精品 目录未找到')
    for e in os.scandir(jingpin):
        if e.is_file() and keyword in e.name and (e.name.endswith('.xlsx')):
            return e.path
    raise FileNotFoundError(f'含 {keyword} 的 xlsx 未找到')

# 主类别映射（售货FAQ xlsx → 7 个 KB 文件）
REFUND_FAQ_CATEGORY_MAP = {
    '退款相 关': 'refund',
    '物流相 关': 'logistics',
    '催发货': 'shipping',
    '错发/ 漏发相 关': 'reissue',
    '结束语': 'greeting',
    '欢迎、 等待、': 'greeting',  # 合并到 greeting
    '售后类': 'after_sales',
}


def normalize_cat(name):
    """cat 累积 + 归一化"""
    if not name:
        return None
    name = name.strip()
    # 去除 xlsx 中额外的 "，名" 等格式问题
    for k in REFUND_FAQ_CATEGORY_MAP:
        if k.replace(' ', '') == name.replace(' ', ''):
            return REFUND_FAQ_CATEGORY_MAP[k]
    return None


def load_refund_faq():
    """读售后FAQ xlsx → 按主类别分 6 组"""
    wb = openpyxl.load_workbook(XLSX_REFUND_FAQ, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c else '' for c in row]
        if not any(cells):
            continue
        rows.append(cells)

    last_cat = ''
    grouped = {k: [] for k in set(REFUND_FAQ_CATEGORY_MAP.values())}
    for cells in rows:
        cat_raw = cells[0] if len(cells) > 0 else ''
        sub = cells[1] if len(cells) > 1 else ''
        script = cells[2] if len(cells) > 2 else ''
        logic = cells[3] if len(cells) > 3 else ''

        if cat_raw and cat_raw not in ('项目', '售后话述FAQ及处理逻辑'):
            mapped = normalize_cat(cat_raw)
            if mapped:
                last_cat = mapped

        if sub == '场景' or (not sub and script == '建议话述'):
            continue
        if not sub and not script and not logic:
            continue
        if not last_cat:
            continue

        grouped[last_cat].append({
            'sub': sub or '',
            'script': script or '',
            'logic': logic or '',
        })

    return grouped


def load_double11():
    """读双11话术 xlsx → 1 组"""
    fp = resolve_xlsx_path('双11活动客服话术')
    wb = openpyxl.load_workbook(fp, data_only=True)
    ws = wb.active
    items = []
    current_section = '场景话术'
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c else '' for c in row]
        if not any(cells):
            continue
        # 表格头 R1
        if cells[0] == '情况':
            continue
        # 第二个表格头 R10
        if cells[0] == '话术类别':
            current_section = '话术模板'
            continue
        # 数据行
        if len(cells) >= 3 and cells[2]:
            items.append({
                'sub': cells[0] or '',
                'script': cells[2],
                'logic': cells[1] or '',
                'section': current_section,
            })
    return items


def load_quick_phrase():
    """读快捷短语 xlsx → 1 组"""
    fp = resolve_xlsx_path('客服售后常用快捷短语汇总')
    wb = openpyxl.load_workbook(fp, data_only=True)
    ws = wb.active
    items = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c else '' for c in row]
        if not any(cells):
            continue
        # 列结构: 总组分类 / 关键词 / 话术
        if len(cells) >= 4 and cells[3]:
                items.append({
                    'sub': cells[2] or '',  # 关键词
                    'script': cells[3],     # 话术
                    'logic': cells[1] or '',  # 分类
                })
    return items


def build_kb_json(category, doc_type, items, source_prefix):
    """生成 KB JSON 格式"""
    out_items = []
    for i, it in enumerate(items, 1):
        title_parts = [it['sub']] if it.get('sub') else []
        if it.get('logic'):
            title_parts.append(it['logic'][:30])
        title = f"{category} - " + " | ".join(p for p in title_parts if p) if title_parts else f"{category}_{i:03d}"

        # Q + A + 处理逻辑 Markdown 拼接
        q_part = f"Q：{it['sub']}怎么办？\n\n" if it.get('sub') else ""
        a_part = f"A：{it['script']}\n\n" if it.get('script') else ""
        l_part = f"## 处理逻辑\n\n{it['logic']}\n" if it.get('logic') else ""
        text = (q_part + a_part + l_part).strip()

        out_items.append({
            'source': f'{source_prefix}_{i:03d}',
            'doc_type': doc_type,
            'title': title[:120],
            'text': text,
        })
    return {
        'category': category,
        'doc_type': doc_type,
        'items': out_items,
    }


def write_kb_file(category, doc_type, items, source_prefix):
    """写 1 个 KB JSON 文件"""
    data = build_kb_json(category, doc_type, items, source_prefix)
    fp = os.path.join(KB_OUT_DIR, f'faq_{category}.json')
    Path(KB_OUT_DIR).mkdir(parents=True, exist_ok=True)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'  ✅ {fp} → {len(data["items"])} items')


def main():
    print('=== 1. 售后FAQ xlsx ===')
    grouped = load_refund_faq()
    for cat, items in grouped.items():
        if not items:
            continue
        write_kb_file(f'xlsx_{cat}', 'faq', items, f'xlsx_{cat}')
        print(f'    category={cat}: {len(items)} items')

    print('\n=== 2. 双11话术 xlsx ===')
    d11 = load_double11()
    write_kb_file('xlsx_promotion', 'supplement_faq', d11, 'xlsx_promotion')
    print(f'    items={len(d11)}')

    print('\n=== 3. 售后快捷短语 xlsx ===')
    qp = load_quick_phrase()
    write_kb_file('xlsx_supplement', 'supplement_faq', qp, 'xlsx_supplement')
    print(f'    items={len(qp)}')

    print('\n✅ 全部写入完成')


if __name__ == '__main__':
    main()
"""Phase 0：Lune 会议目录快照。

调用 Lune ``list_conferences``，对 6 个白名单会议 × 3 个年份（2024/2025/2026）
逐一记录索引状态。未索引必须写 ``not_indexed``，绝不写成「无论文」。

产出：
- snapshots/lune_conferences_{date}.json  （list_conferences 原始返回 + 解析结果）
- config/venues_agents.json                （白名单 × 年度索引状态表）

运行：python3 frontierlitbench/scripts/phase0_snapshot.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import common_lib as lib

sys.path.insert(0, str(lib.ROOT))  # 便于 import 项目根的 mcp_lune / mcp_client

# 加载项目根 .env（支持 LUNE_API_KEY=... 形式，不覆盖已设置的环境变量）
try:
    from common import load_env
    load_env()
except Exception:
    pass


def _credential_kind() -> str:
    """检查 Lune 凭据，返回 'oauth' / 'env-key' / ''。"""
    try:
        import mcp_lune
        token = mcp_lune.access_token()
    except Exception:
        token = ''
    if token:
        return 'oauth'
    if os.getenv('LUNE_API_KEY'):
        return 'env-key'
    return ''


def _as_years(value) -> list:
    """把会议条目里的年份字段（int/str/list）统一成 int 列表。"""
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [int(value)] if 1990 <= int(value) <= 2100 else []
    if isinstance(value, str):
        years = []
        for token in value.replace('-', ' ').split():
            if token.isdigit() and 1990 <= int(token) <= 2100:
                years.append(int(token))
        return years
    if isinstance(value, (list, tuple)):
        years = []
        for item in value:
            years.extend(_as_years(item))
        return sorted(set(years))
    return []


def _extract_items(payload):
    """从 list_conferences 返回中提取会议条目列表（容错多种结构）。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('conferences', 'results', 'data', 'items', 'venues', 'catalog'):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def _parse_coverage(items):
    """解析出 {(venue_name, year), ...} 覆盖集合与原始条目。"""
    coverage = set()
    for item in items:
        if isinstance(item, str):
            coverage.add((item, None))
            continue
        if not isinstance(item, dict):
            continue
        name = (item.get('short_name') or item.get('name') or item.get('conference')
                or item.get('venue') or item.get('venue_name') or item.get('full_name')
                or item.get('title') or '')
        years = item.get('years')
        if years is None:
            years = item.get('year') or item.get('editions')
        year_list = _as_years(years)
        for year in year_list:
            coverage.add((str(name), year))
        if not year_list:
            coverage.add((str(name), None))
    return coverage


def _venue_matched(name: str, venue: str) -> bool:
    """会议名是否匹配白名单缩写（避免 NAACL/ECML 等子串误配）。"""
    text = str(name or '').upper()
    if venue == 'ACL':
        return text == 'ACL' or 'ANNUAL MEETING OF THE ASSOCIATION FOR COMPUTATIONAL LINGUISTICS' in text
    if venue == 'EMNLP':
        return text == 'EMNLP' or 'EMPIRICAL METHODS IN NATURAL LANGUAGE PROCESSING' in text
    if venue == 'NAACL' or venue == 'EACL':
        return False
    if venue == 'ICML':
        return text == 'ICML' or 'INTERNATIONAL CONFERENCE ON MACHINE LEARNING' in text
    if venue == 'ICLR':
        return text == 'ICLR' or 'INTERNATIONAL CONFERENCE ON LEARNING REPRESENTATIONS' in text
    if venue == 'NeurIPS':
        return text in ('NEURIPS', 'NIPS') or 'NEURAL INFORMATION PROCESSING SYSTEMS' in text
    if venue == 'AAAI':
        return text == 'AAAI' or 'AAAI CONFERENCE ON ARTIFICIAL INTELLIGENCE' in text
    return text == venue


def main() -> int:
    lib.ensure_dirs()
    checked_at = lib.utc_now_iso()
    date_str = datetime.now(timezone.utc).strftime('%Y%m%d')

    credential = _credential_kind()
    if not credential:
        print('错误：未检测到 Lune 凭据。请先设置 LUNE_API_KEY 环境变量后重跑。')
        print('示例：export LUNE_API_KEY="..."')
        return 2

    import mcp_client
    import mcp_lune

    try:
        provider = mcp_lune.LuneProvider(max_requests=1)
        client = provider.client()
        raw_result = client.call_tool('list_conferences', {})
        payload = mcp_client.structured(raw_result)
    except mcp_client.McpError as exc:
        print('错误：调用 list_conferences 失败：%s %s' % (exc.kind, exc.status or ''))
        return 1
    except Exception as exc:
        print('错误：调用 list_conferences 异常：%r' % exc)
        return 1

    items = _extract_items(payload)
    coverage = _parse_coverage(items)

    # 对 6 会议 × 3 年份逐一判断索引状态
    venues = {}
    for venue in lib.TRACK_C_VENUES:
        per_year = {}
        for year in lib.YEARS:
            indexed = any(_venue_matched(name, venue) and yr == year
                          for name, yr in coverage)
            # 也接受「条目无年份但会议被索引」的情况：单独标记为 indexed_no_year
            if not indexed:
                indexed = any(_venue_matched(name, venue) and yr is None
                              for name, yr in coverage)
                per_year[year] = 'indexed' if indexed else 'not_indexed'
                if indexed:
                    per_year[year] = 'indexed'
            else:
                per_year[year] = 'indexed'
        venues[venue] = per_year

    snapshot = {
        'protocol_version': lib.PROTOCOL_VERSION,
        'track': lib.TRACK,
        'checked_at': checked_at,
        'credential': credential,
        'source': 'lune_list_conferences',
        'raw_payload': payload,
        'coverage': sorted([list(pair) for pair in coverage]),
        'venues': venues,
    }
    snapshot_path = lib.SNAPSHOTS_DIR / ('lune_conferences_%s.json' % date_str)
    lib.write_json(snapshot_path, snapshot)

    config_path = lib.CONFIG_DIR / 'venues_agents.json'
    lib.write_json(config_path, {
        'protocol_version': lib.PROTOCOL_VERSION,
        'track': lib.TRACK,
        'whitelist': list(lib.TRACK_C_VENUES),
        'years': list(lib.YEARS),
        'indexed': venues,
        'checked_at': checked_at,
        'source': 'lune_list_conferences',
    })

    lib.log_review('phase0_snapshot', venues=venues, checked_at=checked_at,
                   item_count=len(items))

    print('Phase 0 完成')
    print('会议条目数：%d' % len(items))
    print('索引状态：%s' % json.dumps(venues, ensure_ascii=False))
    print('快照：%s' % snapshot_path)
    print('配置：%s' % config_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Đo shadow từ log API; không lưu câu hỏi, không đổi response.

Dùng: docker logs CONTAINER > /tmp/shadow.log 2>&1
uv run python scripts/eval_router_shadow.py --shadow-log /tmp/shadow.log --set eval/golden_v2.jsonl
uv run python scripts/eval_router_shadow.py --middleware-log /tmp/api.log --set eval/golden_v2.jsonl
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def summarize(rows: list[dict]) -> dict:
    out = {}
    for field in ('media', 'wants_file', 'oos'):
        stats = dict(labelled=0, rules_correct=0, rules_misses=0, llm_preserves=0,
                     llm_fixes=0, llm_unavailable=0, agreement=0)
        for row in rows:
            expected = row['expected'].get(field)
            rules = (row.get('rules') or {}).get(field)
            if expected is None or rules is None:
                continue
            stats['labelled'] += 1
            correct = rules == expected
            stats['rules_correct' if correct else 'rules_misses'] += 1
            llm = row.get('llm')
            if llm is None or field not in llm:
                stats['llm_unavailable'] += 1
                continue
            stats['agreement'] += llm[field] == rules
            if llm[field] == expected:
                stats['llm_preserves' if correct else 'llm_fixes'] += 1
        out[field] = stats
    return out



def expected_labels(case: dict) -> dict:
    """Thiếu dữ liệu trong phạm vi không đồng nghĩa ngoài phạm vi."""
    oos = case.get('oos') if isinstance(case.get('oos'), bool) else None
    if oos is None and case.get('case') == 'out_of_scope':
        oos = True
    elif oos is None and case.get('answerable') is True:
        oos = False
    return {'media': case.get('media_mode'), 'wants_file': case.get('wants_file'), 'oos': oos}


def main() -> None:
    ap = argparse.ArgumentParser()
    logs = ap.add_mutually_exclusive_group(required=True)
    logs.add_argument('--shadow-log')
    logs.add_argument('--middleware-log', help='log quyết định trước rewrite: báo riêng raw và final')
    ap.add_argument('--set', action='append', required=True)
    ap.add_argument('--out')
    args = ap.parse_args()
    marker = 'middleware_route ' if args.middleware_log else 'shadow_route '
    events = {}
    for line in Path(args.middleware_log or args.shadow_log).read_text(encoding='utf-8').splitlines():
        if marker not in line:
            continue
        try:
            item = json.loads(line.split(marker, 1)[1])
            events[item['key']] = item
        except (ValueError, KeyError, TypeError):
            continue
    rows = []
    for file in args.set:
        for line in Path(file).read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            key = hashlib.sha256((case['question'] + '\0' + str(bool(case.get('image')))).encode()).hexdigest()
            event = events.get(key)
            expected = expected_labels(case)
            row = {'id': case['id'], 'expected': expected,
                   'rules': event['rules'] if event else None,
                   'llm': event.get('final' if args.middleware_log else 'llm') if event else None}
            if args.middleware_log:
                row.update(raw_llm=event.get('llm') if event else None,
                           origin=event.get('origin') if event else 'missing')
            rows.append(row)
    matched = sum(row['rules'] is not None for row in rows)
    result = {'total': len(rows), 'matched': matched, 'missing_events': len(rows) - matched,
              'summary': summarize(rows), 'rows': rows}
    if args.middleware_log:
        result['raw_summary'] = summarize([{**r, 'llm': r['raw_llm']} for r in rows
                                           if r['origin'] in ('llm', 'cache', 'fallback')])
        result['origins'] = dict(Counter(r['origin'] for r in rows))
        result['blocked_llm_fixes'] = {}
        result['unconsulted_rule_misses'] = {}
        for field in ('media', 'wants_file', 'oos'):
            misses = [r for r in rows if r['expected'].get(field) is not None
                      and r.get('rules') and r['rules'].get(field) != r['expected'][field]
                      and r.get('llm') and r['llm'].get(field) != r['expected'][field]]
            result['blocked_llm_fixes'][field] = [r['id'] for r in misses
                if r.get('raw_llm') and r['raw_llm'].get(field) == r['expected'][field]]
            result['unconsulted_rule_misses'][field] = [r['id'] for r in misses if r['origin'] == 'rules']
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(output + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, ensure_ascii=False))


if __name__ == '__main__':
    main()

"""Đo shadow từ log API; không lưu câu hỏi, không đổi response.

Dùng: docker logs CONTAINER > /tmp/shadow.log 2>&1
uv run python scripts/eval_router_shadow.py --shadow-log /tmp/shadow.log --set eval/multimodal_v1.jsonl
"""
from __future__ import annotations

import argparse
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
    ap.add_argument('--shadow-log', required=True)
    ap.add_argument('--set', action='append', required=True)
    ap.add_argument('--out')
    args = ap.parse_args()
    events = {}
    for line in Path(args.shadow_log).read_text(encoding='utf-8').splitlines():
        if 'shadow_route ' not in line:
            continue
        try:
            item = json.loads(line.split('shadow_route ', 1)[1])
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
            rows.append({'id': case['id'], 'expected': expected,
                         'rules': event['rules'] if event else None,
                         'llm': event['llm'] if event else None})
    matched = sum(row['rules'] is not None for row in rows)
    result = {'total': len(rows), 'matched': matched, 'missing_events': len(rows) - matched,
              'summary': summarize(rows), 'rows': rows}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(output + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('total', 'matched', 'missing_events', 'summary')}, ensure_ascii=False))


if __name__ == '__main__':
    main()

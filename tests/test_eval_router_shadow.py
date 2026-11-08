from scripts.eval_router_shadow import summarize


def test_agreement_reports_rules_misses_and_preservation():
    rows = [
        {'expected': {'media': 'required'}, 'rules': {'media': 'none'}, 'llm': {'media': 'required'}},
        {'expected': {'media': 'none'}, 'rules': {'media': 'none'}, 'llm': {'media': 'none'}},
        {'expected': {'media': 'none'}, 'rules': {'media': 'none'}, 'llm': None},
    ]
    result = summarize(rows)['media']
    assert result['rules_misses'] == 1 and result['llm_fixes'] == 1
    assert result['rules_correct'] == 2 and result['llm_preserves'] == 1
    assert result['llm_unavailable'] == 1


def test_oos_label_does_not_conflate_in_scope_unanswerable():
    from scripts.eval_router_shadow import expected_labels
    assert expected_labels({'answerable': False, 'case': 'no_answer'})['oos'] is None
    assert expected_labels({'answerable': False, 'case': 'out_of_scope'})['oos'] is True
    assert expected_labels({'answerable': True, 'case': 'fact'})['oos'] is False
    assert expected_labels({'oos': False, 'answerable': False})['oos'] is False


def test_cli_counts_missing_shadow_events_and_unavailable_decisions(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys

    cases = [
        {'id': 'a', 'question': 'one', 'media_mode': 'none'},
        {'id': 'b', 'question': 'two', 'media_mode': 'none'},
    ]
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text(''.join(json.dumps(case) + '\n' for case in cases))
    key = hashlib.sha256(b'one\0False').hexdigest()
    log = tmp_path / 'shadow.log'
    log.write_text('shadow_route ' + json.dumps({'key': key, 'rules': {'media': 'none'}, 'llm': None}) + '\n')
    output = tmp_path / 'result.json'
    subprocess.run([sys.executable, 'scripts/eval_router_shadow.py', '--shadow-log', str(log),
                    '--set', str(dataset), '--out', str(output)], check=True, capture_output=True)
    result = json.loads(output.read_text())
    assert result['total'] == 2
    assert result['matched'] == 1
    assert result['missing_events'] == 1
    assert result['summary']['media']['llm_unavailable'] == 1
    assert result['rows'][1]['id'] == 'b' and result['rows'][1]['rules'] is None


def test_middleware_report_keeps_raw_model_separate_from_effective_route(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys

    cases = [{'id': 'a', 'question': 'download form', 'media_mode': 'none', 'wants_file': True},
             {'id': 'b', 'question': 'show image', 'media_mode': 'required', 'wants_file': False}]
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text(''.join(json.dumps(c) + '\n' for c in cases))
    rules = [{'media': 'none', 'wants_file': True, 'oos': False},
             {'media': 'required', 'wants_file': False, 'oos': False}]
    events = [dict(key=hashlib.sha256((c['question'] + '\0False').encode()).hexdigest(),
                   rules=r, final=r, llm={'media': 'none', 'wants_file': False, 'oos': False} if i == 0 else None,
                   origin='llm' if i == 0 else 'rules') for i, (c, r) in enumerate(zip(cases, rules))]
    log = tmp_path / 'middleware.log'
    log.write_text(''.join('middleware_route ' + json.dumps(e) + '\n' for e in events))
    output = tmp_path / 'report.json'
    proc = subprocess.run([sys.executable, 'scripts/eval_router_shadow.py', '--middleware-log', str(log),
                           '--set', str(dataset), '--out', str(output)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(output.read_text())
    assert result['summary']['wants_file']['llm_preserves'] == 2
    assert result['raw_summary']['wants_file']['rules_correct'] == 1
    assert result['raw_summary']['wants_file']['llm_preserves'] == 0
    assert result['raw_summary']['wants_file']['llm_unavailable'] == 0
    assert result['origins'] == {'llm': 1, 'rules': 1}


def test_middleware_report_identifies_unconsulted_and_blocked_rule_misses(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys

    cases = [{'id': 'a', 'question': 'about regulation', 'media_mode': 'none', 'wants_file': False},
             {'id': 'b', 'question': 'about event', 'media_mode': 'none', 'wants_file': False}]
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text(''.join(json.dumps(c) + '\n' for c in cases))
    rules = [{'media': 'none', 'wants_file': True, 'oos': False},
             {'media': 'optional', 'wants_file': False, 'oos': False}]
    events = [dict(key=hashlib.sha256((c['question'] + '\0False').encode()).hexdigest(), rules=r, final=r,
                   llm={'media': 'none', 'wants_file': False, 'oos': False} if i == 0 else None,
                   origin='llm' if i == 0 else 'rules') for i, (c, r) in enumerate(zip(cases, rules))]
    log = tmp_path / 'middleware.log'
    log.write_text(''.join('middleware_route ' + json.dumps(e) + '\n' for e in events))
    output = tmp_path / 'report.json'
    proc = subprocess.run([sys.executable, 'scripts/eval_router_shadow.py', '--middleware-log', str(log),
                           '--set', str(dataset), '--out', str(output)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(output.read_text())
    assert result['blocked_llm_fixes']['wants_file'] == ['a']
    assert result['unconsulted_rule_misses']['media'] == ['b']

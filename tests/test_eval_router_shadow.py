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

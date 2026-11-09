"""Bộ v2 không có trường intent của bộ v1."""
import json
import sys
from pathlib import Path

import numpy as np


def test_offline_eval_accepts_golden_v2_case(tmp_path, monkeypatch):
    from scripts import eval_retrieval
    from sict_crawler.service.embedding import embedder

    row = json.loads(Path('eval/golden_v2.jsonl').read_text(encoding='utf-8').splitlines()[0])
    assert 'intent' not in row and row.get('case')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'manifest.json').write_text(json.dumps({'text_model': 'test-model'}))
    (artifacts / 'chunks.jsonl').write_text(json.dumps({'text': 'trường công nghệ', 'url': 'https://example.org/test'}) + '\n')
    np.save(artifacts / 'chunks.npy', np.array([[1.0, 0.0]], dtype=np.float32))
    monkeypatch.setattr(embedder, 'embed_query', lambda question: [1.0, 0.0])
    monkeypatch.setattr(embedder, 'get_device', lambda: 'cpu')
    out = tmp_path / 'out'
    monkeypatch.setattr(sys, 'argv', ['eval_retrieval.py', '--artifacts', str(artifacts), '--out', str(out)])
    eval_retrieval.main()
    result = json.loads((out / 'test-model.json').read_text())
    assert row['case'] in result['by_intent']['dense']
    assert len(result['per_question']['dense']) == 88

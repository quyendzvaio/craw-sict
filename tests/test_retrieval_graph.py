import pytest


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch):
    from sict_crawler.repository import cache
    monkeypatch.setattr(cache, 'get', lambda key: None)
    monkeypatch.setattr(cache, 'put', lambda key, value, ttl: None)


from sict_crawler.service.retrieval import router


@pytest.mark.parametrize('question', [
    'Hội nghị khoa học diễn ra ngày nào?',
    'Năm nay có những hội thảo nào?',
    'Trung tâm được thành lập theo quyết định nào?',
    'Có những quy định đào tạo nào?',
    'Tại sao quyết định thành lập trường được ban hành?',
    'File quyết định thành lập trường do ai ký?',
])
def test_hybrid_can_correct_topic_hints_for_factual_questions(monkeypatch, question):
    from sict_crawler.service.retrieval import hybrid_router
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda *a: {
        'media': 'none', 'wants_file': False, 'oos': False, 'confidence': .9})
    result = hybrid_router.route(question, False)
    assert result.media == 'none' and not result.wants_file
    assert result.source == 'llm+rules'


@pytest.mark.parametrize('question', [
    'Tải quy định đào tạo ở đâu?', 'tai quy che dao tao o dau',
    'Quyết định phân công giảng dạy', 'Kế hoạch học kỳ mới',
    'Cho tôi file quyết định và cho biết ai ký?',
    'Mẫu đơn đăng ký lấy ở đâu?', 'Kế hoạch đào tạo xem ở đâu?',
    'Tôi có thể tải quy chế đào tạo ở đâu?',
    'Tìm thời khóa biểu học kỳ này ở đâu?',
    'Quy chế đào tạo tải ở trang nào?',
    'toi co the tai quy che dao tao o dau',
])
def test_hybrid_preserves_document_lookup_despite_negative_proposal(monkeypatch, question):
    from sict_crawler.service.retrieval import hybrid_router
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda *a: {
        'media': 'none', 'wants_file': False, 'oos': False, 'confidence': .9})
    assert hybrid_router.route(question, False).wants_file


def test_hybrid_keeps_image_input_optional_when_model_says_none(monkeypatch):
    from sict_crawler.service.retrieval import hybrid_router
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda *a: {
        'media': 'none', 'wants_file': False, 'oos': False, 'confidence': .9})
    result = hybrid_router.route('Sự kiện trong ảnh này ở đâu?', True)
    assert result.media == 'optional' and 'image' in result.inputs


@pytest.mark.parametrize('question, expected', [
    ('Lịch học', False), ('Gửi tôi tài liệu môn học ở đâu?', True),
])
def test_llm_file_promotion_needs_request_or_document_rule(monkeypatch, question, expected):
    from sict_crawler.service.retrieval import hybrid_router
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda *a: {
        'media': 'none', 'wants_file': True, 'oos': False, 'confidence': .9})
    assert hybrid_router.route(question, False).wants_file is expected


@pytest.mark.parametrize('proposal', [None, {}, {
    'media': 'none', 'wants_file': False, 'oos': False, 'confidence': .3}])
def test_ambiguous_topic_falls_back_without_valid_proposal(monkeypatch, proposal):
    from sict_crawler.service.retrieval import hybrid_router
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda *a: proposal)
    question = 'Có những quy định đào tạo nào?'
    assert hybrid_router.route(question, False) == router.classify_rules(question)


def test_policy_merge_preserves_explicit_rules_and_rejects_weak_or_invalid():
    rules = router.Route(media='required', wants_file=True, inputs=['image'])
    merged = router.policy_merge(rules, {'media': 'none', 'wants_file': False, 'oos': False, 'confidence': 0.9})
    assert merged.media == 'required' and merged.wants_file and merged.inputs == ['image']
    assert router.policy_merge(rules, {'media': 'none', 'wants_file': False, 'oos': True, 'confidence': 0.2}) == rules
    assert router.policy_merge(rules, {'media': 'none', 'wants_file': 'false', 'oos': False, 'confidence': 0.9}) == rules
    oos = router.policy_merge(rules, {'media': 'required', 'wants_file': True, 'oos': True, 'confidence': 0.9})
    assert oos.oos and oos.media == 'none' and oos.wants_file


def test_hybrid_cache_and_image_are_scoped(monkeypatch):
    from sict_crawler.service.retrieval import hybrid_router
    from sict_crawler.core.config import LlmConfig
    cfg = LlmConfig(router_model='flash', api_key='x')
    monkeypatch.setattr(hybrid_router, 'get_settings', lambda: type('S', (), {'llm': cfg, 'retrieval': type('R', (), {'restore_accents': False})()})())
    store, calls = {}, []
    monkeypatch.setattr(hybrid_router.cache, 'get', lambda key: store.get(key))
    monkeypatch.setattr(hybrid_router.cache, 'put', lambda key, data, ttl: store.update({key: data}))
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda q, img: calls.append((q, img)) or {'media': 'optional', 'wants_file': False, 'oos': False, 'confidence': 0.9})
    assert hybrid_router.route('Học phí?', False).media == 'optional'
    assert hybrid_router.route('Học phí?', False).media == 'optional'
    assert hybrid_router.route('Học phí?', True).media == 'optional'
    assert calls == [('Học phí?', False), ('Học phí?', True)]


def test_graph_falls_back_and_preserves_api_contract(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    from sict_crawler.model.schemas import Source
    calls = []
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *args: router.Route(media='none'))
    monkeypatch.setattr(agent.retriever, 'retrieve', lambda *a, **kw: (calls.append(kw), [Source(url='u', title='t', kind='text', score=.9, excerpt='học phí')], [], kw['route_override'])[1:])
    result = graph.run('Học phí?', 5, None, None)
    assert result['attempts'] == 1 and 'context' in result and len(calls) == 1


def test_oos_removes_media_outputs_and_marks_insufficient(monkeypatch):
    from sict_crawler.service.retrieval import agent
    from sict_crawler.model.schemas import Source
    route = router.Route(media='none', oos=True, outputs=['text', 'image'])
    state = {'tsrcs': [Source(url='u', title='t', kind='text', score=.9, excerpt='text')],
             'msrcs': [Source(url='i', title='i', kind='image', score=.9)],
             'route': route, 'attempts': 1, 'log': []}
    result = agent._validate_step('Ngoài phạm vi?', state)
    assert result['sufficient'] is False and result['media'] == []
    assert 'image' not in result['outputs'] and '(image)' not in result['context']


def test_reroute_tries_attachment_once_and_keeps_original_on_failure(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    from sict_crawler.core.config import LlmConfig
    from sict_crawler.model.schemas import Source
    cfg = LlmConfig(router_reroute=True)
    settings = agent.get_settings()
    monkeypatch.setattr(settings, 'llm', cfg)
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: router.Route(media='none', wants_file=True))
    calls = []
    def retrieve(q, k, image, filters, route_override=None):
        calls.append(filters)
        sources = [Source(url='u', title='t', kind='text', score=.9, excerpt='nội dung')] if len(calls) == 1 else []
        return {'tsrcs': sources, 'msrcs': [], 'route': route_override, 'attempts': 1, 'log': []}
    monkeypatch.setattr(agent, '_retrieve_step', retrieve)
    result = graph.run('Xin file?', 5, None, None)
    assert calls == [None, {'doc_types': ['attachment']}]
    assert result['text'][0].url == 'u' and result['attempts'] == 2
    assert 'tài liệu' in result['media_note'].lower()


def test_retry_does_not_replace_good_text_with_unrelated_file(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    from sict_crawler.core.config import LlmConfig
    from sict_crawler.model.schemas import Source
    cfg = LlmConfig(router_reroute=True)
    monkeypatch.setattr(agent.get_settings(), 'llm', cfg)
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: router.Route(media='none', wants_file=True))
    calls = []
    def retrieve(q, k, image, filters, route_override=None):
        calls.append(filters)
        if len(calls) == 1:
            src = Source(url='good', title='Biểu mẫu thực tập', kind='text', score=.9,
                         excerpt='Biểu mẫu thực tập', content='Biểu mẫu thực tập', cosine=.9)
        else:
            src = Source(url='bad', title='Quy chế tuyển sinh', kind='file', score=.01,
                         excerpt='Quy chế tuyển sinh', content='Quy chế tuyển sinh', cosine=.01)
        return {'tsrcs': [src], 'msrcs': [], 'route': route_override, 'attempts': 1, 'log': []}
    monkeypatch.setattr(agent, '_retrieve_step', retrieve)
    result = graph.run('Biểu mẫu thực tập?', 5, None, None)
    assert len(calls) == 2 and result['text'][0].url == 'good'
    assert 'tài liệu' in result['media_note'].lower()


def test_reroute_accepts_supported_file_and_respects_explicit_filter(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    from sict_crawler.core.config import LlmConfig
    from sict_crawler.model.schemas import Source
    monkeypatch.setattr(agent.get_settings(), 'llm', LlmConfig(router_reroute=True))
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: router.Route(media='none', wants_file=True))
    calls = []
    def retrieve(q, k, image, filters, route_override=None):
        calls.append(filters)
        kind = 'text' if len(calls) == 1 else 'file'
        src = Source(url=kind, title='Biểu mẫu thực tập', kind=kind, score=.9,
                     excerpt='Biểu mẫu thực tập', content='Biểu mẫu thực tập', cosine=.9)
        return {'tsrcs': [src], 'msrcs': [], 'route': route_override, 'attempts': 1, 'log': []}
    monkeypatch.setattr(agent, '_retrieve_step', retrieve)
    result = graph.run('Biểu mẫu thực tập?', 5, None, None)
    assert result['text'][0].kind == 'file' and result['attempts'] == 2
    calls.clear()
    result = graph.run('Biểu mẫu thực tập?', 5, None, {'doc_types': ['article']})
    assert calls == [{'doc_types': ['article']}] and result['text'][0].kind == 'text'
    assert 'tài liệu' in result['media_note'].lower()


def test_graph_failure_uses_rules_without_second_llm_call(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    calls = []
    def fail(*args, **kwargs):
        raise RuntimeError('graph failed')
    monkeypatch.setattr(graph._graph, 'invoke', fail)
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: (_ for _ in ()).throw(AssertionError('LLM called')))
    monkeypatch.setattr(agent, '_run_linear', lambda q, k, image, filters, route: calls.append(route) or {'ok': True})
    assert graph.run('Học phí?', 5, None, None) == {'ok': True}
    assert len(calls) == 1 and calls[0].source == 'rules'


def test_oos_chat_refuses_without_calling_answer_llm(monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from sict_crawler.api.routes import app
    from sict_crawler.service.retrieval import agent, vlm
    monkeypatch.setattr(agent, 'run', lambda *a, **k: {
        'text': [], 'media': [], 'context': '', 'attempts': 1, 'sufficient': False,
        'media_mode': 'none', 'wants_file': False, 'oos': True})
    monkeypatch.setattr(vlm, 'stream_answer', lambda *a, **k: (_ for _ in ()).throw(AssertionError('LLM called')))
    with TestClient(app) as client:
        response = client.post('/chat', json={'question': 'Câu ngoài phạm vi?'})
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith('data: ') and line != 'data: [DONE]']
    assert any('ngoài phạm vi' in e.get('delta', '').lower() for e in events)
    assert any('citations' in e for e in events)


def test_graph_result_cache_skips_route_and_retrieval(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    from sict_crawler.model.schemas import Source
    store, calls = {}, []
    monkeypatch.setattr(graph.cache, 'get', lambda k: store.get(k))
    monkeypatch.setattr(graph.cache, 'put', lambda k, value, ttl: store.update({k: value}))
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: calls.append('route') or router.Route())
    def retrieve(*args):
        calls.append('retrieve')
        return {'tsrcs': [Source(url='u', title='t', kind='text', score=.9, excerpt='nội dung')],
                'msrcs': [], 'route': args[-1], 'attempts': 1, 'log': []}
    monkeypatch.setattr(agent, '_retrieve_step', retrieve)
    first = graph.run('Câu hỏi', 5, None, None)
    second = graph.run('Câu hỏi', 5, None, None)
    assert first == second and calls == ['route', 'retrieve']


def test_hybrid_chat_graph_streams_status_sources_deltas_and_citations(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph, vlm
    from sict_crawler.model.schemas import Source
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: router.Route(media='none'))
    src = Source(url='https://example.org/a', title='Học phí', kind='text', score=.9,
                 excerpt='Học phí ngành CNTT', content='Học phí ngành CNTT', cosine=.9)
    monkeypatch.setattr(agent, '_retrieve_step', lambda *a, **k: {
        'tsrcs': [src], 'msrcs': [], 'route': a[-1], 'attempts': 1, 'log': []})
    monkeypatch.setattr(vlm, 'stream_answer', lambda *a: iter(['Học phí ', 'theo thông báo [1].']))
    events = list(graph.stream_chat('Học phí ngành CNTT?', 5, None, None))
    assert 'status' in events[0] and 'text' in events[1]
    assert ''.join(e.get('delta', '') for e in events).startswith('Học phí theo thông báo [1].')
    assert 'citations' in events[-2] and events[-1] == {'done': True}


def test_hybrid_chat_endpoint_forwards_graph_stream(monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from sict_crawler.api.routes import app
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.retrieval import agent, graph
    monkeypatch.setattr(get_settings().llm, 'router_mode', 'hybrid')
    monkeypatch.setattr(agent, 'run', lambda *a: (_ for _ in ()).throw(AssertionError('linear path')))
    monkeypatch.setattr(graph, 'stream_chat', lambda *a: iter([
        {'status': {'attempts': 1}}, {'text': [], 'media': []}, {'delta': 'Xin chào'},
        {'citations': []}, {'done': True}]))
    with TestClient(app) as client:
        response = client.post('/chat', json={'question': 'Hỏi?'})
    assert response.status_code == 200 and 'data: [DONE]' in response.text
    events = [json.loads(x[6:]) for x in response.text.splitlines() if x.startswith('data: ') and x != 'data: [DONE]']
    assert events[2]['delta'] == 'Xin chào' and events[3]['citations'] == []


def test_hybrid_chat_falls_back_when_graph_raises(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph, vlm
    def fail(*a, **k):
        raise RuntimeError('graph failed')
    monkeypatch.setattr(graph._chat_graph, 'stream', fail)
    monkeypatch.setattr(agent, '_run_linear', lambda *a: {
        'text': [], 'media': [], 'context': '', 'attempts': 1, 'sufficient': False,
        'media_mode': 'none', 'wants_file': False, 'oos': False})
    monkeypatch.setattr(vlm, 'stream_answer', lambda *a: iter(['Xin chào']))
    events = list(graph.stream_chat('Hỏi?', 5, None, None))
    assert events[-1] == {'done': True} and any(e.get('delta') == 'Xin chào' for e in events)


def test_hybrid_chat_worker_failure_finishes_sse(monkeypatch):
    from fastapi.testclient import TestClient
    from sict_crawler.api.routes import app
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.retrieval import graph
    monkeypatch.setattr(get_settings().llm, 'router_mode', 'hybrid')
    def fail(*a):
        raise RuntimeError('unexpected failure')
    monkeypatch.setattr(graph, 'stream_chat', fail)
    with TestClient(app) as client:
        response = client.post('/chat', json={'question': 'Hỏi?'})
    assert response.status_code == 200 and 'data: [DONE]' in response.text


def test_graph_cache_tracks_context_budget_and_evidence_threshold(monkeypatch):
    from sict_crawler.service.retrieval import agent, graph
    from sict_crawler.model.schemas import Source
    settings = agent.get_settings()
    monkeypatch.setattr(settings.llm, 'source_chars', 7000)
    monkeypatch.setattr(settings.llm, 'context_chars', 5000)
    store, calls = {}, []
    monkeypatch.setattr(graph.cache, 'get', lambda k: store.get(k))
    monkeypatch.setattr(graph.cache, 'put', lambda k, data, ttl: store.update({k: data}))
    monkeypatch.setattr(graph.hybrid_router, 'route', lambda *a: router.Route())
    src = Source(url='u', title='t', kind='text', score=.9, excerpt='học phí',
                 content='học phí ' * 1000, cosine=.7)
    def retrieve(*args):
        calls.append(1)
        return {'tsrcs': [src], 'msrcs': [], 'route': args[-1], 'attempts': 1, 'log': []}
    monkeypatch.setattr(agent, '_retrieve_step', retrieve)
    first = graph.run('Học phí?', 5, None, None)
    monkeypatch.setattr(settings.llm, 'context_chars', 500)
    second = graph.run('Học phí?', 5, None, None)
    assert len(second['context']) < len(first['context']) and len(calls) == 2
    monkeypatch.setattr(settings.retrieval, 'min_cosine', .9)
    third = graph.run('Học phí?', 5, None, None)
    assert third['sufficient'] is False and len(calls) == 3


def test_middleware_logs_proposal_and_effective_route_without_question(monkeypatch, caplog):
    import json
    from sict_crawler.service.retrieval import hybrid_router
    proposal = {'media': 'none', 'wants_file': False, 'oos': False, 'confidence': .9}
    monkeypatch.setattr(hybrid_router.vlm, 'route_intent', lambda *args: proposal)
    result = hybrid_router.route('Tải quy chế đào tạo ở đâu?', False)
    events = [json.loads(r.message.split('middleware_route ', 1)[1]) for r in caplog.records if 'middleware_route ' in r.message]
    assert len(events) == 1
    assert events[0]['llm'] == proposal
    assert events[0]['final']['wants_file'] is True and result.wants_file
    assert all('Tải quy chế' not in record.message for record in caplog.records)

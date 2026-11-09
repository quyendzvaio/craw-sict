"""Hợp đồng shadow router: LLM hỏng không đổi đường luật."""
import json
from unittest.mock import patch

from sict_crawler.core.config import LlmConfig
from sict_crawler.service.retrieval.route_schema import RouteDecision


def test_no_accent_image_event_noun_phrase_preserves_person_and_fact_questions():
    from sict_crawler.service.retrieval import router
    for question in ("anh khai mac olympic tin hoc sinh vien lan thu 33",
                     "anh khai giang nam 2026", "anh hoi thao khoa hoc nam 2026"):
        assert router.classify_rules(question).media == "required"
    for question in ("anh chi cho em biet lich hoc cua truong",
                     "chuan dau ra tieng anh cua truong",
                     "anh Nguyen Van Nam la giang vien khoa nao",
                     "anh tham gia hoi thao nao",
                     "anh tham gia hoi thao", "anh chi tham gia hoi thao",
                     "anh Nam tham gia hoi thao", "anh Nguyen Van Nam tham gia hoi thao"):
        assert router.classify_rules(question).media != "required"


def test_no_accent_image_event_skips_llm_router(monkeypatch):
    from sict_crawler.service.retrieval import hybrid_router, vlm
    monkeypatch.setattr(vlm, "route_intent", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LLM was called")))
    assert hybrid_router.route("anh khai mac olympic tin hoc sinh vien lan thu 33", False).media == "required"
from sict_crawler.service.retrieval import vlm


def test_schema_validates_all_fields_and_types():
    raw = '{"media":"optional","wants_file":false,"oos":false,"confidence":0.8}'
    assert RouteDecision.from_llm('```json\n' + raw + '\n```').model_dump() == json.loads(raw)
    for bad in ('x', '{}', '{"media":"none","wants_file":false,"oos":false,"confidence":1.5}',
                '{"media":"none","wants_file":"false","oos":false,"confidence":0.9}',
                '{"media":"none","wants_file":false,"oos":false,"confidence":true}',
                '{"media":"none","wants_file":false,"oos":false,"confidence":"0.9"}'):
        assert RouteDecision.from_llm(bad) is None


def test_route_intent_uses_router_model_and_falls_back():
    cfg = LlmConfig(api_key='test', router_model='flash', router_timeout=3)
    with patch.object(vlm, 'get_settings') as settings, patch.object(vlm, '_text_call') as call:
        settings.return_value.llm = cfg
        call.return_value = '{"media":"required","wants_file":false,"oos":false,"confidence":0.9}'
        assert vlm.route_intent('Câu hỏi', True)['media'] == 'required'
        assert call.call_args.args[0] == 'flash'
        assert call.call_args.kwargs['timeout'] == 3
        call.return_value = 'bad json'
        assert vlm.route_intent('Câu hỏi', False) is None
        call.side_effect = TimeoutError()
        assert vlm.route_intent('Câu hỏi', False) is None


def test_shadow_is_off_by_default_and_never_changes_response(monkeypatch):
    from sict_crawler.service.retrieval import agent, router
    from sict_crawler.model.schemas import Source
    cfg = LlmConfig()
    real_settings = agent.get_settings()
    monkeypatch.setattr(real_settings, 'llm', cfg)
    monkeypatch.setattr(agent.retriever, 'retrieve', lambda *a, **k: (
        [Source(url='u', title='t', kind='text', score=0.9, excerpt='học phí')], [],
        router.Route(media='none')))
    submitted = []
    monkeypatch.setattr(agent, '_submit_shadow', lambda *args: submitted.append(args))
    base = agent.run('Học phí?')
    assert not submitted
    cfg.router_shadow = True
    result = agent.run('Học phí?')
    assert submitted and result == base


def test_router_timeout_limits_entire_response(monkeypatch):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            for byte in body:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(.03)
                except (BrokenPipeError, ConnectionResetError):
                    break
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = LlmConfig(api_base=f'http://127.0.0.1:{server.server_port}', api_key='test')
    monkeypatch.setattr(vlm, 'get_settings', lambda: type('S', (), {'llm': cfg})())
    start = time.monotonic()
    try:
        assert vlm._text_call('test', 'prompt', timeout=.2) == ''
        assert time.monotonic() - start < .8
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_submit_failure_does_not_break_request(monkeypatch):
    from sict_crawler.service.retrieval import agent, router
    from sict_crawler.model.schemas import Source
    cfg = LlmConfig(router_shadow=True)
    monkeypatch.setattr(agent.get_settings(), 'llm', cfg)
    monkeypatch.setattr(agent.retriever, 'retrieve', lambda *a, **k: (
        [Source(url='u', title='t', kind='text', score=.9, excerpt='nội dung')], [], router.Route()))
    def broken(*args):
        raise RuntimeError('submit failed')
    monkeypatch.setattr(agent, '_submit_shadow', broken)
    assert agent.run('Câu hỏi?')['text'][0].url == 'u'


def test_hybrid_does_not_dispatch_second_shadow_call(monkeypatch):
    from sict_crawler.service.retrieval import agent, router
    from sict_crawler.model.schemas import Source
    cfg = LlmConfig(router_mode='hybrid', router_shadow=True)
    monkeypatch.setattr(agent.get_settings(), 'llm', cfg)
    monkeypatch.setattr(agent.retriever, 'retrieve', lambda *a, **k: (
        [Source(url='u', title='t', kind='text', score=.9, excerpt='nội dung')], [], k['route_override']))
    called = []
    monkeypatch.setattr(agent, '_submit_shadow', lambda *a: called.append(a))
    agent._retrieve_step('Câu hỏi', 5, None, None, router.Route())
    assert not called


def test_shadow_runs_in_background_and_logs_without_question(monkeypatch, caplog):
    import threading
    import time
    from sict_crawler.service.retrieval import agent, router
    settings = agent.get_settings()
    monkeypatch.setattr(settings.retrieval, 'restore_accents', False)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow(question, has_image):
        started.set()
        release.wait(2)
        return {'media': 'none', 'wants_file': False, 'oos': False, 'confidence': .9}
    monkeypatch.setattr(vlm, 'route_intent', slow)
    original = agent._shadow_log.warning
    def record(*args, **kwargs):
        original(*args, **kwargs)
        finished.set()
    monkeypatch.setattr(agent._shadow_log, 'warning', record)
    start = time.monotonic()
    try:
        agent._submit_shadow('Câu hỏi riêng tư', False, router.Route())
        assert time.monotonic() - start < .5 and started.wait(1)
    finally:
        release.set()
    assert finished.wait(2)
    assert 'shadow_route' in caplog.text and 'Câu hỏi riêng tư' not in caplog.text


def test_shadow_skips_extra_call_when_llm_rewrite_enabled(monkeypatch):
    from sict_crawler.service.retrieval import agent, router
    from sict_crawler.model.schemas import Source
    cfg = LlmConfig(router_mode='shadow', rewrite_query=True)
    monkeypatch.setattr(agent.get_settings(), 'llm', cfg)
    monkeypatch.setattr(agent.retriever, 'retrieve', lambda *a, **k: (
        [Source(url='u', title='t', kind='text', score=.9, excerpt='nội dung')], [], router.Route()))
    called = []
    monkeypatch.setattr(agent, '_submit_shadow', lambda *a: called.append(a))
    agent.run('Câu hỏi')
    assert not called


def test_router_config_environment_and_safe_defaults(monkeypatch):
    for key in ('LLM_ROUTER_MODE', 'LLM_ROUTER_SHADOW', 'LLM_ROUTER_REROUTE', 'LLM_ROUTER_MODEL', 'LLM_ROUTER_TIMEOUT'):
        monkeypatch.delenv(key, raising=False)
    cfg = LlmConfig()
    assert cfg.router_mode == 'rules' and not cfg.router_shadow and not cfg.router_reroute
    monkeypatch.setenv('LLM_ROUTER_MODE', 'shadow')
    monkeypatch.setenv('LLM_ROUTER_TIMEOUT', '4')
    monkeypatch.setenv('LLM_ROUTER_MODEL', 'flash')
    assert LlmConfig().router_mode == 'shadow' and LlmConfig().router_timeout == 4
    monkeypatch.setenv('LLM_ROUTER_MODE', 'bad')
    import pytest
    with pytest.raises(ValueError):
        LlmConfig()


def test_route_prompt_explains_school_scope_and_missing_evidence():
    cfg = LlmConfig(api_key='test', router_model='flash')
    with patch.object(vlm, 'get_settings') as settings, patch.object(vlm, '_text_call') as call:
        settings.return_value.llm = cfg
        call.return_value = '{"media":"none","wants_file":false,"oos":false,"confidence":0.9}'
        vlm.route_intent('Câu hỏi về trường', False)
        prompt = call.call_args.args[1]
        assert 'Phạm vi bao gồm cả thông tin liên quan tới trường HaUI' in prompt
        assert 'thiếu dữ liệu không đồng nghĩa ngoài phạm vi' in prompt


def test_router_budget_can_differ_from_answer_budget(monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_MAX_NEW_TOKENS', '8192')
    cfg = LlmConfig(api_key='test', router_model='flash', max_new_tokens=2048)
    assert cfg.router_max_new_tokens == 8192
    with patch.object(vlm, 'get_settings') as settings, patch.object(vlm, '_text_call') as call:
        settings.return_value.llm = cfg
        call.return_value = '{"media":"none","wants_file":false,"oos":false,"confidence":0.9}'
        vlm.route_intent('Câu hỏi', False)
        assert call.call_args.kwargs['max_tokens'] == 8192
        assert cfg.max_new_tokens == 2048


def test_text_call_override_does_not_change_default_answer_budget():
    cfg = LlmConfig(api_key='test', max_new_tokens=2048)
    with patch.object(vlm, 'get_settings') as settings, patch.object(vlm, '_client') as client:
        settings.return_value.llm = cfg
        response = client.return_value.__enter__.return_value.post.return_value
        response.status_code = 200
        response.text = '{"choices":[{"message":{"content":"ok"}}]}'
        assert vlm._text_call('model', 'prompt', max_tokens=8192) == 'ok'
        assert client.return_value.__enter__.return_value.post.call_args.kwargs['json']['max_tokens'] == 8192
        assert vlm._text_call('model', 'prompt') == 'ok'
        assert client.return_value.__enter__.return_value.post.call_args.kwargs['json']['max_tokens'] == 2048

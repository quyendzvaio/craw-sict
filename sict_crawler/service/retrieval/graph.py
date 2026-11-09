"""Điều phối truy xuất hybrid; mặc định không được kích hoạt."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from typing import Any

from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph

from sict_crawler.core.config import get_settings
from sict_crawler.model.schemas import Source
from sict_crawler.repository import cache
from sict_crawler.service.retrieval import hybrid_router, retriever, router

log = logging.getLogger(__name__)


class RetrievalState(TypedDict, total=False):
    question: str
    top_k: int
    image: str | None
    filters: dict | None
    route: Any
    retrieval: dict
    result: dict
    original_result: dict
    retries: int
    retry: bool
    cache_key: str
    cache_hit: bool



def _cache_lookup(state: RetrievalState) -> dict:
    settings = get_settings()
    cfg = settings.llm
    identity = {
        'question': state['question'], 'top_k': state['top_k'], 'filters': state.get('filters'),
        'image': hashlib.sha256((state.get('image') or '').encode()).hexdigest(),
        'index': retriever.index_tag(), 'model': cfg.router_model or cfg.model, 'provider': cfg.api_base,
        'reroute': cfg.router_reroute, 'router_timeout': cfg.router_timeout,
        'route_policy': hybrid_router.ROUTE_POLICY_VERSION,
        'router_max_tokens': cfg.router_max_new_tokens or cfg.max_new_tokens,
        'embedding': {k: getattr(settings.embedding, k) for k in
                      ('backend', 'model', 'text_model', 'image_model', 'dim', 'query_prefix', 'query_image_size')},
        'source_chars': cfg.source_chars, 'context_chars': cfg.context_chars,
        'list_context_chars': cfg.list_context_chars,
        'retrieval': asdict(settings.retrieval), 'router': asdict(settings.router),
        'graph': asdict(settings.graph),
    }
    key = 'gq:v1:' + hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        raw = cache.get(key)
        if raw:
            result = json.loads(raw)
            result['text'] = [Source(**s) for s in result['text']]
            result['media'] = [Source(**s) for s in result['media']]
            return {'cache_key': key, 'cache_hit': True, 'result': result}
    except Exception:
        pass
    return {'cache_key': key, 'cache_hit': False}


def _cache_next(state: RetrievalState) -> str:
    return END if state['cache_hit'] else 'route'


def _cache_store(state: RetrievalState) -> dict:
    result = dict(state['result'])
    result['text'] = [s.model_dump() for s in result['text']]
    result['media'] = [s.model_dump() for s in result['media']]
    try:
        cache.put(state['cache_key'], json.dumps(result, ensure_ascii=False).encode(), retriever.QUERY_TTL)
    except Exception:
        log.exception('Không thể lưu cache graph')
    return {}


def _route(state: RetrievalState) -> dict:
    if not state['question'].strip() and state.get('image'):
        decision = router.Route(media='required', source='image', inputs=['image'])
    else:
        decision = hybrid_router.route(state['question'], bool(state.get('image')))
    return {'route': decision}


def _retrieve(state: RetrievalState) -> dict:
    from sict_crawler.service.retrieval import agent
    result = agent._retrieve_step(state['question'], state['top_k'], state.get('image'),
                                  state.get('filters'), state['route'])
    return {'retrieval': result}


def _has_file(result: dict) -> bool:
    return any(getattr(source, 'kind', None) == 'file' for source in result['text'])


def _has_supported_file(question: str, result: dict) -> bool:
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.retrieval import agent, sources
    for source in result['text']:
        if (getattr(source, 'kind', None) != 'file' or source.cosine is None
                or source.cosine < get_settings().retrieval.min_cosine):
            continue
        context = sources.build_context([source], [])
        if agent.evidence(question, [source], [], context)['sufficient']:
            return True
    return False


def _validate(state: RetrievalState) -> dict:
    from sict_crawler.core.config import get_settings
    from sict_crawler.service.retrieval import agent
    result = agent._validate_step(state['question'], state['retrieval'])
    route = state['route']
    enabled = get_settings().llm.router_reroute
    retries = state.get('retries', 0)
    has_file = _has_supported_file(state['question'], result) if enabled else _has_file(result)
    missing = enabled and route.wants_file and not route.oos and not has_file
    allowed_types = (state.get('filters') or {}).get('doc_types') or []
    can_retry = not allowed_types or 'attachment' in allowed_types
    already_attachment_only = allowed_types == ['attachment']
    if missing and not retries and can_retry and not already_attachment_only:
        return {'result': result, 'original_result': result, 'retry': True}
    if missing:
        original = state.get('original_result')
        if original is not None:
            result = {**original, 'attempts': original['attempts'] + result['attempts']}
        note = 'Không tìm thấy tài liệu đính kèm phù hợp.'
        result = {**result, 'media_note': ' '.join(filter(None, (result.get('media_note'), note)))}
    elif retries:
        # Nếu thử lại tìm được file, giữ kết quả ấy và công khai số lượt.
        result = {**result, 'attempts': result['attempts'] + state['original_result']['attempts']}
    return {'result': result, 'retry': False}


def _retry(state: RetrievalState) -> dict:
    filters = dict(state.get('filters') or {})
    filters['doc_types'] = ['attachment']
    return {'filters': filters, 'retries': 1}


def _next(state: RetrievalState) -> str:
    return 'retry' if state.get('retry') else 'store'


def _answer_events(question: str, image: str | None, result: dict):
    """Sự kiện SSE thô; node graph và đường dự phòng dùng cùng một hợp đồng."""
    from sict_crawler.service.retrieval import citations, vlm
    text, media, context = result['text'], result['media'], result['context']
    status_fields = ('attempts', 'sufficient', 'media_mode', 'wants_file', 'media_note',
                     'list_question', 'entities', 'inputs', 'outputs')
    yield {'status': {key: result.get(key) for key in status_fields}}
    yield {'text': [source.model_dump(exclude={'content'}) for source in text],
           'media': [source.model_dump(exclude={'content'}) for source in media]}
    if result.get('oos'):
        yield {'delta': 'Câu hỏi nằm ngoài phạm vi thông tin của SICT.'}
        yield {'citations': []}
        yield {'done': True}
        return
    if not result['sufficient']:
        context = '(Lưu ý: tài liệu tìm được chỉ liên quan một phần, câu trả lời có thể chưa đầy đủ.)\n\n' + context
    prompt = question.strip() or 'Người dùng gửi một ảnh, không kèm câu hỏi. Hãy cho biết thông tin liên quan đến ảnh này.'
    # T2 prompt cache: hit → stream lại từ cache, không gọi LLM (spec docs/prompt_caching_multi_tier.md).
    from sict_crawler.service.retrieval.prompt_cache import stream_answer_cached
    pieces = []
    try:
        for chunk in stream_answer_cached(prompt, context, image):
            pieces.append(chunk)
            yield {'delta': chunk}
    except Exception:
        log.exception('LLM trả lời bị lỗi')
    if not pieces:
        fallback = f'(LLM chưa trả lời được — đây là context truy xuất được)\n\n{context[:1500]}'
        for offset in range(0, len(fallback), 80):
            part = fallback[offset:offset + 80]
            pieces.append(part)
            yield {'delta': part}
    said = ''.join(pieces)
    final, cites = citations.enforce(said, text + media)
    if final != said:
        yield {'delta': final[len(said):]} if final.startswith(said) else {'answer': final}
    yield {'citations': cites}
    yield {'done': True}


def _answer(state: RetrievalState) -> dict:
    from langgraph.config import get_stream_writer
    writer = get_stream_writer()
    for event in _answer_events(state['question'], state.get('image'), state['result']):
        writer(event)
    return {}


def _build(chat: bool = False):
    builder = StateGraph(RetrievalState)
    builder.add_node('cache', _cache_lookup)
    builder.add_node('store', _cache_store)
    builder.add_node('route', _route)
    builder.add_node('retrieve', _retrieve)
    builder.add_node('validate', _validate)
    builder.add_node('retry', _retry)
    if chat:
        builder.add_node('answer', _answer)
    builder.add_edge(START, 'cache')
    if chat:
        builder.add_conditional_edges('cache', lambda state: 'answer' if state['cache_hit'] else 'route',
                                      {'route': 'route', 'answer': 'answer'})
    else:
        builder.add_conditional_edges('cache', _cache_next, {'route': 'route', END: END})
    builder.add_edge('route', 'retrieve')
    builder.add_edge('retrieve', 'validate')
    builder.add_conditional_edges('validate', _next, {'retry': 'retry', 'store': 'store'})
    builder.add_edge('store', 'answer' if chat else END)
    if chat:
        builder.add_edge('answer', END)
    builder.add_edge('retry', 'retrieve')
    return builder.compile()


_graph = _build()
_chat_graph = _build(chat=True)


def run(question: str, top_k: int, image: str | None, filters: dict | None) -> dict:
    try:
        return _graph.invoke({'question': question, 'top_k': top_k, 'image': image,
                              'filters': filters}, config={'recursion_limit': 12})['result']
    except Exception:
        # Không gọi lại LLM khi dự phòng; giữ luật và chữ ký API cũ.
        log.exception('Graph lỗi, dùng tuyến luật dự phòng')
        from sict_crawler.service.retrieval import agent
        rules = router.classify_rules(question, has_image=bool(image))
        if not question.strip() and image:
            rules = router.Route(media='required', source='image', inputs=['image'])
        return agent._run_linear(question, top_k, image, filters, rules)


def stream_chat(question: str, top_k: int, image: str | None, filters: dict | None):
    """Sinh sự kiện thật qua graph.stream, giữ đường dự phòng không gọi router LLM lần hai."""
    emitted = False
    try:
        for event in _chat_graph.stream({'question': question, 'top_k': top_k, 'image': image,
                                         'filters': filters}, config={'recursion_limit': 12},
                                        stream_mode='custom'):
            emitted = True
            yield event
    except Exception:
        log.exception('Graph chat lỗi')
        if emitted:
            yield {'delta': 'Không thể hoàn tất câu trả lời.'}
            yield {'citations': []}
            yield {'done': True}
        else:
            from sict_crawler.service.retrieval import agent
            rules = router.classify_rules(question, has_image=bool(image))
            if not question.strip() and image:
                rules = router.Route(media='required', source='image', inputs=['image'])
            result = agent._run_linear(question, top_k, image, filters, rules)
            yield from _answer_events(question, image, result)

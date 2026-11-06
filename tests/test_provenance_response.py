"""Provenance: /query và event `status` của /chat mang index_version; cache cũ thiếu trường không làm crash."""
import asyncio
import json
from unittest.mock import patch

from sict_crawler.api import routes
from sict_crawler.core import fingerprint
from sict_crawler.model.schemas import QueryIn
from sict_crawler.service.retrieval import agent, vlm

FAKE = {"text": [], "media": [], "context": "", "attempts": 1, "sufficient": True,
        "index_version": {"fingerprint": "demo"}}


def test_result_carries_index_version():
    res = agent._result([], [], "ctx", 1, True, [], None)
    assert res["index_version"] == fingerprint.index_version()
    assert res["index_version"]["fingerprint"] == fingerprint.index_fingerprint()


def test_query_response_carries_index_version():
    with patch.object(agent, "run", return_value=FAKE):
        out = asyncio.run(routes.query(QueryIn(question="x", top_k=3)))
    assert out["index_version"] == {"fingerprint": "demo"}


def test_query_response_tolerates_missing_index_version():
    """Cache/attempt cũ không có trường → vẫn trả response, index_version None (Review Focus 4)."""
    old = {k: v for k, v in FAKE.items() if k != "index_version"}
    with patch.object(agent, "run", return_value=old):
        out = asyncio.run(routes.query(QueryIn(question="x", top_k=3)))
    assert out["index_version"] is None


def test_chat_status_event_carries_index_version():
    async def collect() -> str:
        resp = await routes.chat(QueryIn(question="x", top_k=3))
        chunks = [c if isinstance(c, str) else c.decode() async for c in resp.body_iterator]
        return "".join(chunks)

    with patch.object(agent, "run", return_value=FAKE), \
            patch.object(vlm, "stream_answer", lambda *a, **k: iter(["ok"])):
        body = asyncio.run(collect())
    first = body.split("\n\n", 1)[0]
    assert first.startswith("data: ")
    assert json.loads(first[6:])["status"]["index_version"] == {"fingerprint": "demo"}

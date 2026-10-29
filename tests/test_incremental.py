"""Crawl incremental (2026-10-18): tải lại mọi mục danh sách bất kể frontier:seen (lỗi 1), đi tiếp trang
2, 3… khi còn bài lạ (lỗi 2), thử lại URL tải lỗi tạm thời ở lượt sau (lỗi 3). Redis / site / DB giả."""
import asyncio
import fnmatch
from contextlib import asynccontextmanager

import pytest
import redis

from sict_crawler.core.config import get_settings
from sict_crawler.pipeline import incremental as I

H = "https://sict.haui.edu.vn"


class FakeRedis:
    """Đủ lệnh mà frontier.Frontier dùng."""

    def __init__(self):
        self.sets, self.lists, self.hashes = {}, {}, {}

    def sismember(self, k, v): return v in self.sets.get(k, set())
    def sadd(self, k, v): self.sets.setdefault(k, set()).add(v)
    def scard(self, k): return len(self.sets.get(k, set()))
    def sscan_iter(self, k, match=None, count=None): return iter(list(self.sets.get(k, set())))
    def rpush(self, k, v): self.lists.setdefault(k, []).append(v)
    def llen(self, k): return len(self.lists.get(k, []))
    def lindex(self, k, i): return self.lists.get(k, [])[i]
    def lset(self, k, i, v): self.lists[k][i] = v

    def lrem(self, k, n, v):
        self.lists[k].remove(v)

    def hget(self, k, f): return self.hashes.get(k, {}).get(f)
    def hset(self, k, f, v): self.hashes.setdefault(k, {})[f] = v
    def hdel(self, k, f): self.hashes.get(k, {}).pop(f, None)
    def hgetall(self, k): return dict(self.hashes.get(k, {}))

    def hincrby(self, k, f, n=1):
        h = self.hashes.setdefault(k, {})
        h[f] = int(h.get(f, 0)) + n
        return h[f]

    def delete(self, *ks):
        for k in ks:
            for d in (self.sets, self.lists, self.hashes):
                d.pop(k, None)

    def pipeline(self):
        r = self

        class P:
            def __init__(self): self.ops = []
            def sadd(self, *a): self.ops.append(("sadd", a))
            def rpush(self, *a): self.ops.append(("rpush", a))
            def execute(self): [getattr(r, n)(*a) for n, a in self.ops]
        return P()


def page(*hrefs: str) -> str:
    links = "".join(f'<a href="{h}">x</a>' for h in hrefs)
    menu = '<a href="/vn/tin-tuc">menu</a><a href="/vn/thong-bao">menu</a>'   # menu: trang danh sách
    return f"<html><head><title>T</title></head><body>{menu}{links}</body></html>"


@pytest.fixture
def env(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: fake))
    cfg = get_settings()
    monkeypatch.setattr(cfg.crawl, "delay_seconds", 0)
    monkeypatch.setattr(cfg.site, "seed_urls", [f"{H}/vn/tin-tuc"])
    site: dict[str, tuple[str, int]] = {}
    fetched: list[str] = []

    async def fetch(url):
        fetched.append(url)
        return site.get(url, ("", 404))
    monkeypatch.setattr(I.crawler, "fetch", fetch)
    state = {"doc_urls": []}

    class Session:
        async def scalars(self, stmt):
            class R:
                def all(_): return list(state["doc_urls"])
            return R()

        async def scalar(self, stmt): return None
        async def rollback(self): pass
        async def commit(self): pass

    @asynccontextmanager
    async def get_session():
        yield Session()
    monkeypatch.setattr(I.db, "get_session", get_session)

    async def init_db(): pass
    monkeypatch.setattr(I.db, "init_db", init_db)
    monkeypatch.setattr(I.vector_store, "ensure_collections", lambda: None)
    monkeypatch.setattr(I.storage, "Storage", lambda: None)
    return {"redis": fake, "site": site, "fetched": fetched, "state": state, "cfg": cfg}


def _queued(fake) -> list[str]:
    return [x.split("|", 1)[1] for x in fake.lists.get("frontier:list", [])]


def test_listing_without_seed_is_refreshed_even_if_seen(env):
    asyncio.run(_test_listing_without_seed_is_refreshed_even_if_seen(env))


async def _test_listing_without_seed_is_refreshed_even_if_seen(env):
    """Lỗi 1: mục tuyen-dung không phải seed, trang 1 của nó đã nằm trong seen → vẫn tải lại, bắt bài mới."""
    fake, site = env["redis"], env["site"]
    env["state"]["doc_urls"] = [f"{H}/vn/tuyen-dung/bai-cu/1000"]          # mục rút ra từ bài trong DB
    fake.sadd("frontier:seen", f"{H}/vn/tuyen-dung")                       # đã tải 1 lần ở lượt trước
    site[f"{H}/vn/tin-tuc"] = (page(), 200)
    site[f"{H}/vn/tuyen-dung"] = (page("/vn/tuyen-dung/bai-moi/1001", "/vn/tuyen-dung/bai-cu/1000"), 200)
    site[f"{H}/vn/tuyen-dung/2"] = (page("/vn/tuyen-dung/bai-cu/1000"), 200)
    st = await I.refresh_listings()
    assert f"{H}/vn/tuyen-dung" in env["fetched"]
    assert _queued(fake) == [f"{H}/vn/tuyen-dung/bai-moi/1001"]           # bài đã có trong DB không bị đẩy lại
    assert st["new"] == 1 and st["roots"] == 2


def test_pagination_continues_while_new_and_stops(env):
    asyncio.run(_test_pagination_continues_while_new_and_stops(env))


async def _test_pagination_continues_while_new_and_stops(env):
    """Lỗi 2: > 1 trang bài mới giữa 2 lượt → đi tiếp trang 2, 3; dừng ở trang đầu tiên không còn bài lạ."""
    fake, site = env["redis"], env["site"]
    env["state"]["doc_urls"] = [f"{H}/vn/tin-tuc/cu/1"]
    site[f"{H}/vn/tin-tuc"] = (page("/vn/tin-tuc/a/11", "/vn/tin-tuc/b/12"), 200)
    site[f"{H}/vn/tin-tuc/2"] = (page("/vn/tin-tuc/c/13"), 200)
    site[f"{H}/vn/tin-tuc/3"] = (page("/vn/tin-tuc/cu/1"), 200)
    site[f"{H}/vn/tin-tuc/4"] = (page("/vn/tin-tuc/rat-cu/0"), 200)
    st = await I.refresh_listings()
    assert sorted(_queued(fake)) == [f"{H}/vn/tin-tuc/a/11", f"{H}/vn/tin-tuc/b/12", f"{H}/vn/tin-tuc/c/13"]
    assert f"{H}/vn/tin-tuc/4" not in env["fetched"] and st["pages"] == 3   # trang 3 hết bài lạ → dừng

    fake.delete("frontier:list", "frontier:seen")
    env["fetched"].clear()
    st = await I.refresh_listings(max_pages=2)                             # chặn trên số trang
    assert f"{H}/vn/tin-tuc/3" not in env["fetched"] and st["pages"] == 2


def test_pending_url_is_not_counted_as_new(env):
    asyncio.run(_test_pending_url_is_not_counted_as_new(env))


async def _test_pending_url_is_not_counted_as_new(env):
    """URL đã thấy (đang chờ trong hàng đợi) không tính là bài lạ → không phân trang vô ích."""
    fake, site = env["redis"], env["site"]
    fake.sadd("frontier:seen", f"{H}/vn/tin-tuc/a/11")
    # cột bên: bài mục khác + trang tĩnh chưa gặp → vào hàng đợi nhưng KHÔNG kéo phân trang của tin-tuc
    site[f"{H}/vn/tin-tuc"] = (page("/vn/tin-tuc/a/11", "/vn/thong-bao/khac/99", "/vn/html/gioi-thieu"), 200)
    site[f"{H}/vn/tin-tuc/2"] = (page("/vn/tin-tuc/b/12"), 200)
    st = await I.refresh_listings()
    assert f"{H}/vn/tin-tuc/2" not in env["fetched"]
    assert sorted(_queued(fake)) == [f"{H}/vn/html/gioi-thieu", f"{H}/vn/thong-bao/khac/99"] and st["new"] == 2


def test_transient_failure_is_retried_next_run(env, monkeypatch):
    asyncio.run(_test_transient_failure_is_retried_next_run(env, monkeypatch))


async def _test_transient_failure_is_retried_next_run(env, monkeypatch):
    """Lỗi 3: timeout / 5xx / thân rỗng → lượt sau thử lại; thành công thì xoá bộ đếm lỗi."""
    fake, site = env["redis"], env["site"]
    done = []

    async def process(session, st, url, depth, frontier, html):
        done.append(url)
        return "new"
    monkeypatch.setattr(I, "_process_page", process)
    url = f"{H}/vn/tin-tuc/a/11"
    I.Frontier().push(url, 1)
    site[url] = ("", 0)                                                    # lỗi tạm thời
    st = await I.drain_queue(10)
    assert st == {"processed": 0, "failed": 1, "gave_up": 0, "requeued": 0, "left": 0}
    assert fake.hgetall("frontier:retry") == {url: "1"} and fake.llen("frontier:list") == 0

    site[url] = (page(), 200)                                              # lượt sau site đã ổn
    st = await I.drain_queue(10)
    assert st["requeued"] == 1 and st["processed"] == 1 and done == [url]
    assert fake.hgetall("frontier:fails") == {} and fake.hgetall("frontier:retry") == {}


def test_gives_up_after_max_retries_and_404_is_final(env, monkeypatch):
    asyncio.run(_test_gives_up_after_max_retries_and_404_is_final(env, monkeypatch))


async def _test_gives_up_after_max_retries_and_404_is_final(env, monkeypatch):
    fake, site = env["redis"], env["site"]
    monkeypatch.setattr(env["cfg"].crawl, "max_fetch_retries", 3)

    async def process(*a):
        raise AssertionError("không được xử lý")
    monkeypatch.setattr(I, "_process_page", process)
    bad, gone = f"{H}/vn/tin-tuc/loi/1", f"{H}/vn/tin-tuc/xoa/2"
    I.Frontier().push(bad, 1)
    I.Frontier().push(gone, 1)
    site[bad] = ("", 0)
    results = [await I.drain_queue(10) for _ in range(3)]
    assert [r["failed"] for r in results] == [1, 1, 1] and results[-1]["gave_up"] == 1
    assert fake.hgetall("frontier:retry") == {}                            # bỏ hẳn sau 3 lần
    assert (await I.drain_queue(10))["requeued"] == 0
    assert gone not in fake.hgetall("frontier:fails")                      # 404/410 dứt điểm, không thử lại


def test_processing_error_is_retried(env, monkeypatch):
    asyncio.run(_test_processing_error_is_retried(env, monkeypatch))


async def _test_processing_error_is_retried(env, monkeypatch):
    """Tải được nhưng xử lý lỗi (VD Qdrant tạm sập) → cũng thử lại lượt sau, không mất URL."""
    fake, site = env["redis"], env["site"]

    async def boom(*a):
        raise RuntimeError("qdrant down")
    monkeypatch.setattr(I, "_process_page", boom)
    url = f"{H}/vn/tin-tuc/a/11"
    I.Frontier().push(url, 1)
    site[url] = (page(), 200)
    st = await I.drain_queue(10)
    assert st["failed"] == 1 and fake.hgetall("frontier:retry") == {url: "1"}


def test_known_doc_urls_are_never_requeued(env):
    asyncio.run(_test_known_doc_urls_are_never_requeued(env))


async def _test_known_doc_urls_are_never_requeued(env):
    """Bài nạp qua loader chưa có trong frontier:seen → link tới chúng từng bị tải lại ("unchanged")
    mỗi khi có bài mới. mark_known_seen đưa URL bài trong DB vào seen (dạng chuẩn hoá)."""
    known = f"{H}/vn/tin-tuc/cu/1"
    env["state"]["doc_urls"] = [f"http://sict.haui.edu.vn/vn/tin-tuc/cu/1/"]
    assert await I.mark_known_seen() == 1 and await I.mark_known_seen() == 0
    f = I.Frontier()
    assert not f.push(known, 2) and f.push(f"{H}/vn/tin-tuc/moi/2", 2)
    assert _queued(env["redis"]) == [f"{H}/vn/tin-tuc/moi/2"]


def test_drain_and_recheck_stop_at_deadline(env, monkeypatch):
    asyncio.run(_test_drain_and_recheck_stop_at_deadline(env, monkeypatch))


async def _test_drain_and_recheck_stop_at_deadline(env, monkeypatch):
    """Hết ngân sách thời gian → drain dừng, URL còn nguyên trong hàng đợi cho lượt sau; recheck không tải gì."""
    fake, site = env["redis"], env["site"]
    done = []

    async def process(session, st, url, depth, frontier, html):
        done.append(url)
        return "new"
    monkeypatch.setattr(I, "_process_page", process)
    urls = [f"{H}/vn/tin-tuc/a/{i}" for i in range(3)]
    for u in urls:
        I.Frontier().push(u, 1)
        site[u] = (page(), 200)
    passed = I.time.monotonic() - 1
    st = await I.drain_queue(10, passed)
    assert st["processed"] == 0 and st["left"] == 3 and done == []
    env["state"]["doc_urls"] = [1, 2]                                      # id bài cũ nhất
    assert (await I.recheck_edits(10, passed))["checked"] == 0 and env["fetched"] == []
    st = await I.drain_queue(10, I.time.monotonic() + 60)
    assert st["processed"] == 3 and st["left"] == 0 and done == urls


def test_new_posts_drain_before_recheck_with_one_deadline(monkeypatch):
    asyncio.run(_test_new_posts_drain_before_recheck_with_one_deadline(monkeypatch))


async def _test_new_posts_drain_before_recheck_with_one_deadline(monkeypatch):
    """Bài mới (drain) chạy trước recheck và cả hai dùng chung 1 hạn chót tính từ đầu lượt."""
    calls = []

    async def stage(name, ret):
        calls.append(name)
        return ret

    async def noop(): pass
    monkeypatch.setattr(I.db, "init_db", noop)
    monkeypatch.setattr(I.vector_store, "ensure_collections", lambda: None)
    monkeypatch.setattr(I.vector_store, "has_sparse", lambda: True)
    monkeypatch.setattr(I, "mark_known_seen", lambda: stage("known", 0))
    monkeypatch.setattr(I, "discover_new", lambda: stage("discover", 0))
    monkeypatch.setattr(I, "refresh_listings", lambda: stage("listings", {}))
    seen = {}

    def drain(limit, deadline):
        seen["drain"] = deadline
        return stage("drain", {"processed": 0})

    def recheck(limit, deadline):
        seen["recheck"] = deadline
        return stage("recheck", {})
    monkeypatch.setattr(I, "drain_queue", drain)
    monkeypatch.setattr(I, "recheck_edits", recheck)
    monkeypatch.setattr(I, "_graph_stage", lambda: stage("graph", None))
    monkeypatch.setattr(I, "_maybe_reconcile", lambda: stage("reconcile", None))
    monkeypatch.setattr(I, "_heartbeat", lambda ok, s: None)
    monkeypatch.setattr(I, "_notify", lambda *a: None)
    t0 = I.time.monotonic()
    await I.run_incremental(check_edits=5, crawl_limit=5, annotate_ocr=0, budget_sec=300)
    assert calls == ["known", "discover", "listings", "drain", "recheck", "graph", "reconcile"]
    assert seen["drain"] == seen["recheck"] and t0 + 299 < seen["drain"] <= I.time.monotonic() + 300
    calls.clear()
    await I.run_incremental(check_edits=5, crawl_limit=5, annotate_ocr=0, budget_sec=0)
    assert seen["drain"] is None and seen["recheck"] is None              # 0 = không giới hạn


def test_listing_root():
    assert I.listing_root(f"{H}/vn/tuyen-dung/bai/71781") == f"{H}/vn/tuyen-dung"
    assert I.listing_root(f"http://sict.haui.edu.vn/vn/tin-tuc/3/") == f"{H}/vn/tin-tuc"
    assert I.listing_root(f"{H}/vn/html/gioi-thieu") is None and I.listing_root(f"{H}/vn") is None
    assert fnmatch.fnmatch(I.listing_root(f"{H}/vn/thong-bao/x/1"), "*/vn/thong-bao")

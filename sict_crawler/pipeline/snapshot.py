"""Offline snapshot crawl — chỉ TẢI (HTML + ảnh vùng nội dung + danh sách video), không embed/OCR.

Tách nặng/nhẹ (plan.md): snapshot chạy CPU ở đâu cũng được (giới hạn bởi mạng + politeness);
bước compute nặng (parse → chunk → embed → OCR → keyframe) chạy sau trên máy GPU, đọc snapshot
này. Incremental về sau vẫn chạy inline trên CPU (main.py --incremental) vì dữ liệu mới ít.

Phạm vi: chỉ host của site.base_url (không external_hosts); skip_patterns + max_depth từ config.
Phân trang listing (/vn/<mục>/<n>) giữ nguyên depth của trang cha — site chỉ link tới trang kế
tiếp nên nếu tính depth thì chỉ đi được ~max_depth trang mỗi mục, bỏ sót bài cũ.
Trang listing không tải ảnh (thumbnail trùng ảnh trong bài chi tiết).

Layout <out>/:
  state.sqlite   frontier + trạng thái từng URL/media — ngắt giữa chừng, chạy lại lệnh là resume
  html/<ab>/<sha256(url)>.html.gz      (bytes gốc server trả về)
  media/<ab>/<sha256(bytes)>.<ext>
  pages.jsonl    {url, final_url, depth, page_type, http_status, title, html_path, fetched_at,
                  content_fallback, error_page, images:[{url, alt}], videos:[url]}
  images.jsonl   {url, sha256, path, bytes, content_type}
  videos.jsonl   {url, video_id, pages:[url]}
  files/<ab>/<sha256(bytes)>.<ext>     file đính kèm (pdf/doc/xls/ppt) link từ vùng nội dung
  files.jsonl    {url, sha256, path, bytes, content_type, pages:[{url, desc}]}
  summary.json   + snapshot.log

Usage:
  python -m sict_crawler.pipeline.snapshot --out _snapshot/2026-10-12
  python -m sict_crawler.pipeline.snapshot --out _snapshot/2026-10-12 --export-only
Phase: 1 trang → 2 ảnh → 3 đính kèm (quét lại HTML đã lưu nên snapshot cũ cũng bổ sung được).
Callers: CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from sict_crawler.core.config import get_settings
from sict_crawler.repository.frontier import canonicalize
from sict_crawler.service.crawl import parser
from sict_crawler.service.crawl.urls import is_error_page, link_depth, page_type, youtube_id

RETRY_STATUSES = {0, 408, 425, 429, 500, 502, 503, 504}
MAX_TRIES = 4
HOST_DEAD_AFTER = 5   # lỗi kết nối liên tiếp cùng host → bỏ mọi URL còn chờ của host đó
_CONNECT_ERRORS = ("ConnectTimeout", "ConnectError", "name resolution", "CERTIFICATE_VERIFY")
BACKOFF_BASE = 30.0  # giây; 30, 60, 120
MIN_IMAGE_BYTES = 1000
MAX_IMAGES_PER_PAGE = 40
MAX_FILE_BYTES = 60 * 1024 * 1024
_FILE_TYPES = ("application/pdf", "application/msword", "application/vnd.", "application/octet-stream",
               "application/zip", "binary/octet-stream")
_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
        "image/webp": "webp", "image/svg+xml": "svg", "image/bmp": "bmp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, depth INT, status TEXT DEFAULT 'pending', next_at REAL DEFAULT 0,
  tries INT DEFAULT 0, http_status INT, final_url TEXT, page_type TEXT, html_path TEXT,
  title TEXT, fetched_at TEXT, content_fallback INT, error_page INT, error TEXT);
CREATE TABLE IF NOT EXISTS media(
  url TEXT PRIMARY KEY, status TEXT DEFAULT 'pending', next_at REAL DEFAULT 0,
  tries INT DEFAULT 0, http_status INT, sha256 TEXT, path TEXT, bytes INT,
  content_type TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS files(
  url TEXT PRIMARY KEY, status TEXT DEFAULT 'pending', next_at REAL DEFAULT 0,
  tries INT DEFAULT 0, http_status INT, sha256 TEXT, path TEXT, bytes INT,
  content_type TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS occ(
  page_url TEXT, media_url TEXT, kind TEXT, alt TEXT, pos INT,
  PRIMARY KEY(page_url, media_url));
CREATE INDEX IF NOT EXISTS pages_queue ON pages(status, next_at, depth);
CREATE INDEX IF NOT EXISTS media_queue ON media(status, next_at);
CREATE INDEX IF NOT EXISTS files_queue ON files(status, next_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Scope:
    def __init__(self, base_url: str, skip_patterns: list[str], max_depth: int):
        self.host = urlparse(base_url).netloc.lower()
        self.skip = [re.compile(p) for p in skip_patterns]
        self.max_depth = max_depth

    def accept(self, url: str, depth: int) -> bool:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.netloc.lower() != self.host:
            return False
        if depth > self.max_depth:
            return False
        return not any(rx.search(url) for rx in self.skip)


class Throttle:
    """Khoảng cách tối thiểu giữa 2 request tới cùng host."""

    def __init__(self, delay: float):
        self.delay = delay
        self.last: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = self.last.get(host, 0.0) + self.delay - time.monotonic()
        if gap > 0:
            await asyncio.sleep(gap)
        self.last[host] = time.monotonic()


class Snapshot:
    def __init__(self, out: Path, delay: float, max_pages: int | None,
                 transport: httpx.AsyncBaseTransport | None = None):
        cfg = get_settings()
        self.cfg = cfg
        self.transport = transport  # test: httpx.MockTransport
        self.out = out
        self.max_pages = max_pages
        out.mkdir(parents=True, exist_ok=True)
        # WAL: script đọc song song (kiểm tra, eval) không chặn tiến trình ghi ("database is locked")
        self.db = sqlite3.connect(out / "state.sqlite", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.scope = Scope(cfg.site.base_url, cfg.site.skip_patterns, cfg.crawl.max_depth)
        self.throttle = Throttle(delay)
        self.host_fail: dict[str, int] = {}  # host → số lỗi kết nối liên tiếp
        self.log_f = open(out / "snapshot.log", "a", encoding="utf-8")

    def log(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        print(line, flush=True)
        self.log_f.write(line + "\n")
        self.log_f.flush()

    # ---------- queue ----------
    def normalize_state(self) -> int:
        """Gộp URL trùng do canonicalize cũ (http/https, "/" cuối). Ưu tiên bản đã 'done';
        bản thừa → status 'duplicate' (giữ dòng để audit, không crawl lại). Trả số dòng đổi."""
        changed = 0
        rows = self.db.execute("SELECT url, status FROM pages").fetchall()
        groups: dict[str, list[tuple[str, str]]] = {}
        for url, st in rows:
            groups.setdefault(canonicalize(url), []).append((url, st))
        for canon, items in groups.items():
            if len(items) == 1 and items[0][0] == canon:
                continue
            keep = next((u for u, st in items if st == "done"), None) or \
                next((u for u, _ in items if u == canon), items[0][0])
            for u, st in items:
                if u != keep and st != "duplicate":
                    self.db.execute("UPDATE pages SET status='duplicate', error=? WHERE url=?",
                                    (f"dup-of:{keep}", u))
                    changed += 1
            if keep != canon and not any(u == canon for u, _ in items):
                self.db.execute("UPDATE pages SET url=? WHERE url=?", (canon, keep))
                self.db.execute("UPDATE occ SET page_url=? WHERE page_url=?", (canon, keep))
                changed += 1
        self.db.commit()
        return changed

    def seed(self) -> None:
        for u in self.cfg.site.seed_urls:
            u = canonicalize(u)
            if self.scope.accept(u, 0):
                self.db.execute("INSERT OR IGNORE INTO pages(url, depth) VALUES(?, 0)", (u,))
        self.db.commit()

    def _next(self, table: str, order: str):
        row = self.db.execute(
            f"SELECT url, {order} FROM {table} WHERE status='pending' AND next_at<=? "
            f"ORDER BY {order}, rowid LIMIT 1", (time.time(),)).fetchone()
        if row:
            return row
        wait = self.db.execute(
            f"SELECT MIN(next_at) FROM {table} WHERE status='pending'").fetchone()[0]
        return ("__wait__", wait) if wait is not None else None

    def _host_unreachable(self, table: str, url: str, status: int, err: str) -> bool:
        """Host chết (domain cũ, DNS, SSL hỏng): sau HOST_DEAD_AFTER lỗi kết nối liên tiếp, đánh
        dấu mọi URL còn chờ của host là failed thay vì chờ timeout × MAX_TRIES cho từng URL."""
        host = urlparse(url).netloc
        if status != 0 or not any(e in err for e in _CONNECT_ERRORS):
            self.host_fail[host] = 0
            return False
        self.host_fail[host] = self.host_fail.get(host, 0) + 1
        if self.host_fail[host] < HOST_DEAD_AFTER:
            return False
        n = self.db.execute(
            f"UPDATE {table} SET status='failed', error=? WHERE status='pending' "
            f"AND (url LIKE ? OR url LIKE ?)",
            (f"host-unreachable: {err[:120]}", f"http://{host}/%", f"https://{host}/%")).rowcount
        self.db.commit()
        self.log(f"host-unreachable {host}: bỏ {n} URL {table} còn chờ")
        return True

    def _retry_or_fail(self, table: str, url: str, status: int, err: str) -> None:
        if table != "pages" and self._host_unreachable(table, url, status, err):
            return
        tries = self.db.execute(f"SELECT tries FROM {table} WHERE url=?", (url,)).fetchone()[0] + 1
        if status in RETRY_STATUSES and tries < MAX_TRIES:
            self.db.execute(
                f"UPDATE {table} SET tries=?, http_status=?, error=?, next_at=? WHERE url=?",
                (tries, status, err, time.time() + BACKOFF_BASE * 2 ** (tries - 1), url))
        else:
            self.db.execute(
                f"UPDATE {table} SET tries=?, http_status=?, error=?, status=? WHERE url=?",
                (tries, status, err, "gone" if status in (404, 410) else "failed", url))
        self.db.commit()

    async def _get(self, client: httpx.AsyncClient, url: str):
        await self.throttle.wait(url)
        try:
            r = await client.get(url)
            return r.status_code, r, ""
        except httpx.HTTPError as e:
            return 0, None, f"{type(e).__name__}: {e}"[:300]

    # ---------- pages ----------
    async def crawl_pages(self, client: httpx.AsyncClient) -> None:
        done = self.db.execute("SELECT COUNT(*) FROM pages WHERE status='done'").fetchone()[0]
        while self.max_pages is None or done < self.max_pages:
            nxt = self._next("pages", "depth")
            if nxt is None:
                break
            if nxt[0] == "__wait__":
                await asyncio.sleep(max(0.5, nxt[1] - time.time()))
                continue
            url, depth = nxt
            status, r, err = await self._get(client, url)
            if r is None or status != 200:
                self._retry_or_fail("pages", url, status, err or f"http {status}")
                continue
            if "html" not in r.headers.get("content-type", ""):
                self.db.execute("UPDATE pages SET status='skipped', http_status=?, error=? "
                                "WHERE url=?", (status, "non-html", url))
                self.db.commit()
                continue
            self._store_page(url, depth, r)
            done += 1
            if done % 25 == 0:
                q = self.db.execute("SELECT COUNT(*) FROM pages WHERE status='pending'").fetchone()[0]
                self.log(f"pages done={done} queue={q} last={url}")

    def _store_page(self, url: str, depth: int, r: httpx.Response) -> None:
        h = _sha(url.encode())
        rel = f"html/{h[:2]}/{h}.html.gz"
        (self.out / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.out / rel).write_bytes(gzip.compress(r.content))
        final = str(r.url)
        parsed = parser.parse_html(final, r.text)
        ptype = page_type(final)
        err_page = is_error_page(parsed.markdown)
        if parsed.content_fallback:
            self.log(f"WARN content_fallback {url}")
        for link in parsed.links:
            link = canonicalize(link)
            d = link_depth(depth, link)
            if self.scope.accept(link, d):
                self.db.execute("INSERT OR IGNORE INTO pages(url, depth) VALUES(?, ?)", (link, d))
        if ptype == "content" and not err_page:
            imgs = list(zip(parsed.image_urls, parsed.image_alts))[:MAX_IMAGES_PER_PAGE]
            for pos, (img, alt) in enumerate(imgs):
                if not img.startswith(("http://", "https://")):
                    continue
                self.db.execute("INSERT OR IGNORE INTO occ VALUES(?, ?, 'image', ?, ?)",
                                (url, img, alt, pos))
                self.db.execute("INSERT OR IGNORE INTO media(url) VALUES(?)", (img,))
            for pos, vid in enumerate(parsed.video_urls):
                self.db.execute("INSERT OR IGNORE INTO occ VALUES(?, ?, 'video', '', ?)",
                                (url, vid, pos))
            self._record_files(url, parsed.attachments)
        self.db.execute(
            "UPDATE pages SET status='done', http_status=200, final_url=?, page_type=?, "
            "html_path=?, title=?, fetched_at=?, content_fallback=?, error_page=?, error=NULL "
            "WHERE url=?",
            (final, ptype, rel, parsed.title[:512], _now(), int(parsed.content_fallback),
             int(err_page), url))
        self.db.commit()

    # ---------- media ----------
    async def fetch_media(self, client: httpx.AsyncClient) -> None:
        done = 0
        while True:
            nxt = self._next("media", "tries")
            if nxt is None:
                break
            if nxt[0] == "__wait__":
                await asyncio.sleep(max(0.5, nxt[1] - time.time()))
                continue
            url = nxt[0]
            status, r, err = await self._get(client, url)
            if r is None or status != 200:
                self._retry_or_fail("media", url, status, err or f"http {status}")
                continue
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            data = r.content
            if not ctype.startswith("image/") or len(data) < MIN_IMAGE_BYTES:
                self.db.execute("UPDATE media SET status='skipped', http_status=?, "
                                "content_type=?, bytes=?, error=? WHERE url=?",
                                (status, ctype, len(data), "not-image-or-tiny", url))
                self.db.commit()
                continue
            sha = _sha(data)
            ext = _EXT.get(ctype) or (Path(urlparse(url).path).suffix.lstrip(".").lower()[:4] or "bin")
            rel = f"media/{sha[:2]}/{sha}.{ext}"
            if not (self.out / rel).exists():
                (self.out / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.out / rel).write_bytes(data)
            self.db.execute("UPDATE media SET status='done', http_status=200, sha256=?, path=?, "
                            "bytes=?, content_type=?, error=NULL WHERE url=?",
                            (sha, rel, len(data), ctype, url))
            self.db.commit()
            done += 1
            if done % 100 == 0:
                q = self.db.execute("SELECT COUNT(*) FROM media WHERE status='pending'").fetchone()[0]
                self.log(f"media done={done} queue={q}")

    # ---------- files (đính kèm) ----------
    def _record_files(self, page_url: str, attachments: list[tuple[str, str]]) -> None:
        for pos, (furl, desc) in enumerate(attachments):
            if not furl.startswith(("http://", "https://")):
                continue
            self.db.execute("INSERT OR IGNORE INTO occ VALUES(?, ?, 'file', ?, ?)",
                            (page_url, furl, desc, pos))
            self.db.execute("INSERT OR IGNORE INTO files(url) VALUES(?)", (furl,))

    def collect_files(self) -> int:
        """Quét lại HTML các trang nội dung đã lưu → ghi nhận link đính kèm (idempotent)."""
        before = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        rows = self.db.execute(
            "SELECT url, final_url, html_path FROM pages WHERE status='done' "
            "AND page_type='content' AND COALESCE(error_page, 0)=0").fetchall()
        for url, final, rel in rows:
            try:
                html = gzip.decompress((self.out / rel).read_bytes()).decode("utf-8", "ignore")
            except OSError:
                continue
            self._record_files(url, parser.parse_html(final or url, html).attachments)
        self.db.commit()
        return self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] - before

    async def fetch_files(self, client: httpx.AsyncClient) -> None:
        done = 0
        while True:
            nxt = self._next("files", "tries")
            if nxt is None:
                break
            if nxt[0] == "__wait__":
                await asyncio.sleep(max(0.5, nxt[1] - time.time()))
                continue
            url = nxt[0]
            status, r, err = await self._get(client, url)
            if r is None or status != 200:
                self._retry_or_fail("files", url, status, err or f"http {status}")
                continue
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            data = r.content
            if ctype.startswith("text/html") or not data or len(data) > MAX_FILE_BYTES \
                    or not (ctype.startswith(_FILE_TYPES) or not ctype):
                self.db.execute("UPDATE files SET status='skipped', http_status=?, "
                                "content_type=?, bytes=?, error=? WHERE url=?",
                                (status, ctype, len(data), "not-a-file-or-too-big", url))
                self.db.commit()
                continue
            sha = _sha(data)
            ext = Path(urlparse(url).path).suffix.lstrip(".").lower()[:5] or "bin"
            rel = f"files/{sha[:2]}/{sha}.{ext}"
            if not (self.out / rel).exists():
                (self.out / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.out / rel).write_bytes(data)
            self.db.execute("UPDATE files SET status='done', http_status=200, sha256=?, path=?, "
                            "bytes=?, content_type=?, error=NULL WHERE url=?",
                            (sha, rel, len(data), ctype, url))
            self.db.commit()
            done += 1
            if done % 25 == 0:
                q = self.db.execute("SELECT COUNT(*) FROM files WHERE status='pending'").fetchone()[0]
                self.log(f"files done={done} queue={q}")

    # ---------- export ----------
    def export(self) -> dict:
        db = self.db
        occ: dict[str, dict[str, list]] = {}
        for page_url, media_url, kind, alt, _pos in db.execute(
                "SELECT page_url, media_url, kind, alt, pos FROM occ ORDER BY page_url, kind, pos"):
            d = occ.setdefault(page_url, {"images": [], "videos": []})
            if kind == "image":
                d["images"].append({"url": media_url, "alt": alt})
            else:
                d["videos"].append(media_url)
        cols = ["url", "final_url", "depth", "page_type", "http_status", "title", "html_path",
                "fetched_at", "content_fallback", "error_page"]
        with open(self.out / "pages.jsonl", "w", encoding="utf-8") as f:
            for row in db.execute(f"SELECT {', '.join(cols)} FROM pages WHERE status='done' "
                                  "ORDER BY rowid"):
                rec = dict(zip(cols, row))
                rec["content_fallback"] = bool(rec["content_fallback"])
                rec["error_page"] = bool(rec["error_page"])
                rec.update(occ.get(rec["url"], {"images": [], "videos": []}))
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(self.out / "images.jsonl", "w", encoding="utf-8") as f:
            for url, sha, path, size, ctype in db.execute(
                    "SELECT url, sha256, path, bytes, content_type FROM media "
                    "WHERE status='done' ORDER BY rowid"):
                f.write(json.dumps({"url": url, "sha256": sha, "path": path, "bytes": size,
                                    "content_type": ctype}, ensure_ascii=False) + "\n")
        vids: dict[str, dict] = {}
        for page_url, media_url in db.execute(
                "SELECT page_url, media_url FROM occ WHERE kind='video' ORDER BY rowid"):
            key = youtube_id(media_url) or media_url
            v = vids.setdefault(key, {"url": media_url, "video_id": youtube_id(media_url),
                                      "pages": []})
            v["pages"].append(page_url)
        with open(self.out / "videos.jsonl", "w", encoding="utf-8") as f:
            for v in vids.values():
                f.write(json.dumps(v, ensure_ascii=False) + "\n")

        fpages: dict[str, list] = {}
        for page_url, furl, desc in db.execute(
                "SELECT page_url, media_url, alt FROM occ WHERE kind='file' ORDER BY rowid"):
            fpages.setdefault(furl, []).append({"url": page_url, "desc": desc})
        with open(self.out / "files.jsonl", "w", encoding="utf-8") as f:
            for url, sha, path, size, ctype in db.execute(
                    "SELECT url, sha256, path, bytes, content_type FROM files "
                    "WHERE status='done' ORDER BY rowid"):
                f.write(json.dumps({"url": url, "sha256": sha, "path": path, "bytes": size,
                                    "content_type": ctype, "pages": fpages.get(url, [])},
                                   ensure_ascii=False) + "\n")

        def count(sql: str) -> int:
            return db.execute(sql).fetchone()[0]

        summary = {
            "finished_at": _now(),
            "base_url": self.cfg.site.base_url,
            "pages": {s: n for s, n in db.execute(
                "SELECT status, COUNT(*) FROM pages GROUP BY status")},
            "pages_by_type": {t or "?": n for t, n in db.execute(
                "SELECT page_type, COUNT(*) FROM pages WHERE status='done' GROUP BY page_type")},
            "content_fallback": count("SELECT COUNT(*) FROM pages WHERE content_fallback=1"),
            "error_pages": count("SELECT COUNT(*) FROM pages WHERE error_page=1"),
            "media": {s: n for s, n in db.execute(
                "SELECT status, COUNT(*) FROM media GROUP BY status")},
            "unique_images": count("SELECT COUNT(DISTINCT sha256) FROM media WHERE status='done'"),
            "image_bytes": count("SELECT COALESCE(SUM(bytes), 0) FROM "
                                 "(SELECT DISTINCT sha256, bytes FROM media WHERE status='done')"),
            "image_occurrences": count("SELECT COUNT(*) FROM occ WHERE kind='image'"),
            "videos": len(vids),
            "files": {st: n for st, n in db.execute(
                "SELECT status, COUNT(*) FROM files GROUP BY status")},
        }
        (self.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    async def run(self) -> dict:
        n = self.normalize_state()
        if n:
            self.log(f"normalize_state: gộp {n} URL trùng (http/https, '/' cuối)")
        self.seed()
        cfg = self.cfg.crawl
        async with httpx.AsyncClient(
                timeout=cfg.timeout_seconds, follow_redirects=True,
                headers={"User-Agent": cfg.user_agent},
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                transport=self.transport) as client:
            self.log("phase 1/3: pages")
            await self.crawl_pages(client)
            self.log("phase 2/3: media")
            await self.fetch_media(client)
            self.log(f"phase 3/3: files (+{self.collect_files()} link mới từ HTML đã lưu)")
            await self.fetch_files(client)
        summary = self.export()
        self.log(f"DONE {json.dumps(summary, ensure_ascii=False)}")
        return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline snapshot crawl (không embed/OCR)")
    ap.add_argument("--out", required=True, help="thư mục snapshot (resume nếu đã có)")
    ap.add_argument("--delay", type=float, default=None,
                    help="giây giữa 2 request cùng host (mặc định crawl.delay_seconds)")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--export-only", action="store_true")
    args = ap.parse_args()
    delay = args.delay if args.delay is not None else get_settings().crawl.delay_seconds
    snap = Snapshot(Path(args.out), delay, args.max_pages)
    if args.export_only:
        print(json.dumps(snap.export(), ensure_ascii=False, indent=2))
        return
    asyncio.run(snap.run())


if __name__ == "__main__":
    main()

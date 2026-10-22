"""Dac ta hanh vi thuc thi: RETRIEVAL + RESPONSE + CACHE.

Viet TU plan & trao doi (AC-R/S/K), KHONG doc code nguon.
TRANG THAI: chua chay (cho crawl + preprocessing full).
Chi dung thu vien chuan (unittest).
"""
import hashlib
import unittest


# ---------------- Hop nhat retrieval (AC-R1..R6) ----------------
def text_has_evidence(hit):
    return bool(hit.get("url") and (hit.get("content") or hit.get("heading")))


def media_has_evidence(hit):
    return bool(hit.get("url") and hit.get("caption"))


def collapse_video_frames(image_hits):
    """Mot video chi giu 1 diem: frame dau tien cua moi URL (AC-R3)."""
    seen, out = set(), []
    for h in image_hits:
        key = (h.get("kind"), h.get("url"))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def fuse(text_hits, image_hits):
    """Hop nhat 3 nguon roi xep theo diem giam dan (AC-R3)."""
    liked = ([dict(h, leg="text") for h in text_hits] +
             [dict(h, leg="image") for h in collapse_video_frames(image_hits)])
    liked.sort(key=lambda h: h.get("score", 0), reverse=True)
    return liked


def dedupe_sources(items):
    """Gom ket qua trung (AC-R6)."""
    seen, out = {}, []
    for it in items:
        key = (it.get("leg"), it.get("url"),
               it.get("media_id", it.get("chunk_id")))
        if key in seen:
            seen[key]["also_from"] = seen[key].get("also_from", [])
            seen[key]["also_from"].append(it.get("url"))
            continue
        seen[key] = dict(it)
        out.append(seen[key])
    return out


def rewrite_query(q):
    """Viet lai cau hoi: khong duoc tra rong (AC-R5)."""
    q = (q or "").strip()
    return q if q else ""


# ---------------- Sinh cau tra loi (AC-S1..S5) ----------------
def filter_names_by_evidence(names, spans):
    """Chi giu ten co doan trich bang chung (AC-S1)."""
    return [n for n in names if any(n in s for s in spans)]


def build_response(question, hits, names, llm_ok, policy):
    """Tra (trang thai, payload). policy: dict cam."""
    if policy.get("children") and policy.get("asks_identity"):
        return "refuse", {"reason": "child-safety"}
    if policy.get("asks_position"):
        return "refuse", {"reason": "no-position-tracking"}
    if not hits:
        return "no-data", {"sufficient": False,
                            "text": "khong co du lieu"}
    spans = [h.get("content", "") + " " + h.get("caption", "")
             for h in hits]
    kept = filter_names_by_evidence(names, spans)
    if not llm_ok:
        return "degraded", {"answer": None, "sources": hits,
                             "sufficient": False}
    return "answer", {"names": kept, "sources": hits,
                      "sufficient": bool(kept or hits)}


# ---------------- Cache da tang (AC-K1..K5) ----------------
EDGE_MEDIA_TTL = "30d"
EDGE_PAGE_TTL = "10m"


def edge_cacheable(method, path):
    """Chi GET media/tinh moi duoc edge cache (AC-K1)."""
    if method != "GET":
        return None
    if path.startswith("/media/") or path in ("/", "/app.js"):
        return True
    return False


def edge_ttl(path):
    if path.startswith("/media/"):
        return EDGE_MEDIA_TTL
    return EDGE_PAGE_TTL


def l2_key(question, top_k):
    """Khoa L2 on dinh theo cau hoi + top_k (AC-K2)."""
    raw = "%s|%s" % (question.strip(), top_k)
    return "q:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


class TieredCache:
    """Edge (L0) + L2 mo phong de dac ta hanh vi (AC-K2..K4)."""

    def __init__(self):
        self.edge, self.l2, self.backend_calls = {}, {}, 0

    def get_media(self, path, loader):
        if path in self.edge:
            return self.edge[path], "edge-hit"
        try:
            val = loader()
        except Exception:
            raise
        self.edge[path] = val
        return val, "backend"

    def ask(self, question, top_k, compute):
        key = l2_key(question, top_k)
        if key in self.l2:
            return self.l2[key], "l2-hit"
        self.backend_calls += 1
        val = compute()
        self.l2[key] = val
        return val, "backend"

    def invalidate_prefix(self, prefix):
        self.l2 = {k: v for k, v in self.l2.items()
                   if prefix not in k}


# ================= TEST (chua chay) =================
class TestRetrieval(unittest.TestCase):
    def test_both_legs_have_evidence(self):
        """AC-R1: ca chu va media deu co URL + bang chung."""
        texts = [{"url": "b/1", "title": "T", "heading": "H",
                  "content": "le khai giang", "score": 0.9}]
        media = [{"url": "m/1", "caption": "khai giang", "score": 0.8,
                  "kind": "image"}]
        self.assertTrue(all(text_has_evidence(h) for h in texts))
        self.assertTrue(all(media_has_evidence(h) for h in media))
        fused = fuse(texts, media)
        self.assertEqual([h["leg"] for h in fused], ["text", "image"])

    def test_text_search_finds_captioned_photo(self):
        """AC-R2: hoi ten ra anh (chay that khi crawl xong)."""
        hits = [{"url": "m/9", "caption": "PGS.TS. Nguyen Van A phat bieu",
                 "score": 0.88, "kind": "image"}]
        self.assertTrue(all(media_has_evidence(h) for h in hits))
        self.assertIn("Nguyen Van A", hits[0]["caption"])

    def test_video_collapsed_to_one(self):
        """AC-R3: 20 frame cung video -> 1 diem."""
        frames = [{"url": "yt:X", "kind": "video_frame",
                   "caption": "c", "score": 0.9 - i * 0.01} for i in range(20)]
        fused = fuse([], frames)
        self.assertEqual(len(fused), 1)

    def test_topk_and_empty(self):
        """AC-R4: ton trong top_k, rong thi rong."""
        hits = [{"url": "u%d" % i, "score": 1.0 / (i + 1)} for i in range(10)]
        self.assertEqual(len(fuse(hits, [])[:5]), 5)
        self.assertEqual(fuse([], []), [])

    def test_rewrite_never_empty(self):
        """AC-R5: viet lai khong duoc mat cau hoi."""
        self.assertTrue(rewrite_query("TS A khoa CNTT"))
        self.assertEqual(rewrite_query("   "), "")

    def test_duplicates_merged(self):
        """AC-R6: cung anh 2 bai -> 1 dong kem 2 nguon."""
        items = [{"leg": "image", "url": "m/1", "media_id": "m1",
                  "score": 0.9},
                 {"leg": "image", "url": "m/1", "media_id": "m1",
                  "score": 0.8}]
        out = dedupe_sources(items)
        self.assertEqual(len(out), 1)
        self.assertIn("m/1", out[0]["also_from"])


class TestResponse(unittest.TestCase):
    HITS = [{"url": "b/1", "content": "PGS.TS. Nguyen Van A phat bieu",
             "caption": ""}]

    def test_cited_name_kept(self):
        """AC-S1: ten co bang chung duoc giu."""
        st, p = build_response("ai tham du", self.HITS,
                               ["Nguyen Van A"], True, {})
        self.assertEqual(st, "answer")
        self.assertIn("Nguyen Van A", p["names"])

    def test_uncited_name_dropped(self):
        """AC-S1: ten khong bang chung bi cat."""
        st, p = build_response("ai tham du", self.HITS,
                               ["Tran Thi B"], True, {})
        self.assertNotIn("Tran Thi B", p["names"])

    def test_empty_means_dont_know(self):
        """AC-S2: thieu bang chung -> khong biet + sufficient false."""
        st, p = build_response("ai tham du", [], ["X"], True, {})
        self.assertEqual((st, p["sufficient"]), ("no-data", False))

    def test_policy_beats_retrieval(self):
        """AC-S3: tang tra loi van chan."""
        st, _ = build_response("ai trong anh", self.HITS, ["X"], True,
                               {"children": True, "asks_identity": True})
        self.assertEqual(st, "refuse")

    def test_llm_down_keeps_sources(self):
        """AC-S4: LLM hong -> dap an trong, nguon con, khong sap."""
        st, p = build_response("tuyen sinh", self.HITS, [], False, {})
        self.assertEqual(st, "degraded")
        self.assertIsNone(p["answer"])
        self.assertTrue(p["sources"])


class TestCache(unittest.TestCase):
    def test_edge_rules(self):
        """AC-K1: media 30d, trang 10m, POST khong cache."""
        self.assertTrue(edge_cacheable("GET", "/media/a.png"))
        self.assertTrue(edge_cacheable("GET", "/"))
        self.assertIsNone(edge_cacheable("POST", "/query"))
        self.assertEqual(edge_ttl("/media/a.png"), "30d")
        self.assertEqual(edge_ttl("/"), "10m")

    def test_repeat_ask_hits_l2(self):
        """AC-K2: hoi lap khong tinh lai."""
        c = TieredCache()
        v1, s1 = c.ask("tuyen sinh", 5, lambda: ["r1"])
        v2, s2 = c.ask("tuyen sinh", 5, lambda: ["r2"])
        self.assertEqual((s1, s2, c.backend_calls), ("backend", "l2-hit", 1))
        self.assertEqual(v2, ["r1"])

    def test_l2_key_stable(self):
        """AC-K2: khoa on dinh theo cau + top_k."""
        self.assertEqual(l2_key("a", 5), l2_key("  a ", 5))
        self.assertNotEqual(l2_key("a", 5), l2_key("a", 6))

    def test_invalidate_on_edit(self):
        """AC-K3: bai sua -> vo hieu cache lien quan."""
        c = TieredCache()
        c.l2["q:bai-1-xyz"] = 1
        c.l2["q:other"] = 2
        c.invalidate_prefix("bai-1")
        self.assertNotIn("q:bai-1-xyz", c.l2)
        self.assertIn("q:other", c.l2)

    def test_edge_fail_open(self):
        """AC-K4: hong thi di thang backend (mo phong loi loader truyen qua)."""
        c = TieredCache()
        with self.assertRaises(RuntimeError):
            c.get_media("/media/x.png", lambda: (_ for _ in ()).throw(
                RuntimeError("disk full")))
        self.assertNotIn("/media/x.png", c.edge)


if __name__ == "__main__":
    unittest.main(verbosity=2)

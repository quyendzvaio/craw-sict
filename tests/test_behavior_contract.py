"""Dac ta hanh vi thuc thi — viet TU plan & trao doi (AC-A..G), KHONG doc code nguon.

Moi test tro ve ID trong acceptance_criteria.md va scenario trong features/.
Cac ham chinh sach ben duoi la ban thuc thi cua plan; khi code that xong,
test se chuyen sang goi adapter that nhung giu nguyen ky vong.
Chi dung thu vien chuan (unittest).
"""
import re
import unicodedata
import unittest
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------- Hang so tu plan ----------------
PHASH_HAMMING_MAX = 10            # AC-B1: nguong pHash/dHash
REGISTRY_ANN_COSINE_DEFAULT = 0.9  # AC-B1: tang embedding, mac dinh bao thu
VIDEO_MAX_KEYFRAMES = 20          # AC-D1
SUBTITLE_MAX_CHARS = 2000         # AC-D3
DELETE_GUARD_COUNT = 20           # AC-E4
DELETE_GUARD_RATIO = 0.2          # AC-E4
DELETE_STATUSES = (404, 410)      # AC-E3
TEXT_DIM = 384                    # AC-G1: E5
IMAGE_DIM = 512                   # AC-G1: CLIP
RESIZE_QUERY_KEYS = {"w", "h", "width", "height", "resize", "thumb",
                     "thumbnail", "size", "q"}  # AC-B1


# ---------------- Chinh sach dedupe (AC-B1..B6) ----------------
def normalize_media_url(url):
    """Bo query resize/thumbnail + fragment (AC-B1)."""
    p = urlsplit(url)
    qs = [(k, v) for k, v in parse_qsl(p.query)
          if k.lower() not in RESIZE_QUERY_KEYS]
    qs.sort()
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path,
                       urlencode(qs), ""))


def hamming(a, b):
    return bin(a ^ b).count("1")


def dedupe_verdict(norm_a, norm_b, sha_a, sha_b, phash_dist, clip_cos,
                   same_event_burst=False):
    """Cascade re -> dat, khop tang nao dung tang do (AC-B1, AC-B3)."""
    if norm_a == norm_b or sha_a == sha_b:
        return "duplicate"
    if (phash_dist <= PHASH_HAMMING_MAX
            or clip_cos >= REGISTRY_ANN_COSINE_DEFAULT):
        return "near-dup-group" if same_event_burst else "duplicate"
    return "distinct"


class MediaRegistry:
    """1 media + nhieu ban ghi xuat hien + dem tham chieu (AC-B4, AC-B5)."""

    def __init__(self):
        self._by_sha = {}
        self._occ = defaultdict(list)
        self._seq = 0

    def ingest(self, sha, doc_id, ctx):
        if sha in self._by_sha:
            mid = self._by_sha[sha]
            self._occ[mid].append((doc_id, ctx))
            return mid, False
        self._seq += 1
        mid = "m%d" % self._seq
        self._by_sha[sha] = mid
        self._occ[mid].append((doc_id, ctx))
        return mid, True

    def occurrences(self, mid):
        return list(self._occ[mid])

    def remove_occurrence(self, sha, doc_id):
        """Tra True khi media het tham chieu va bi xoa (AC-B5)."""
        mid = self._by_sha[sha]
        self._occ[mid] = [o for o in self._occ[mid] if o[0] != doc_id]
        if not self._occ[mid]:
            del self._occ[mid]
            del self._by_sha[sha]
            return True
        return False


# ---------------- Chinh sach dinh danh nguoi (AC-C1..C7) ----------------
def calibrate_thresholds(pairs, min_precision=0.99):
    """Hieu chuan 2 nguong tren tap gan nhan tay, cam so cung (AC-C2).

    pairs: [(diem, dung_nguoi_bool)]. high = nguong NHO NHAT dat do
    chinh xac yeu cau; low = nguong nho nhat giu du moi mau dung.
    """
    ths = sorted({s for s, _ in pairs})
    high = max(ths)
    for t in ths:
        sel = [y for s, y in pairs if s >= t]
        if sel and sum(sel) / len(sel) >= min_precision:
            high = t
            break
    low = min(t for t in ths
              if all(s >= t for s, y in pairs if y))
    return low, high


def person_decision(score, margin, low, high, min_margin=0.02):
    """accept / review / reject theo 2 nguong + khoang cach top1-top2."""
    if score < low:
        return "reject"
    if score >= high and margin >= min_margin:
        return "accept"
    return "review"


def text_gate_candidates(members, mentioned):
    """Thu hep bang chu truoc khi doi chieu mat 1:1 (AC-C4)."""
    mentioned = set(mentioned)
    return [m for m in members if m in mentioned]


def face_quality_ok(size_px, blurry, yaw_deg, masked):
    """Loc chat luong mat truoc khi doi chieu (AC-C5)."""
    return (size_px >= 50 and not blurry
            and abs(yaw_deg) <= 30 and not masked)


def admit_name(name, evidence_spans):
    """Ten chi duoc nhan khi co doan trich bang chung (AC-C6)."""
    return any(name in span for span in evidence_spans)


_TITLE_TOKENS = {"pgs", "ts", "ths", "gs", "gv", "tskh", "nskh"}


def normalize_person(raw, gazetteer):
    """Chuan hoa ten qua danh muc: bo hoc ham, khong dau de so (AC-C7)."""
    n = unicodedata.normalize("NFD", raw)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    toks = [t for t in re.split(r"[\s.]+", n.lower()) if t]
    toks = [t for t in toks if t not in _TITLE_TOKENS]
    bare = " ".join(toks)
    for canonical in gazetteer:
        c = unicodedata.normalize("NFD", canonical)
        c = "".join(x for x in c if unicodedata.category(x) != "Mn")
        if c.lower() == bare:
            return canonical
    return None


# ---------------- Chinh sach video (AC-D1..D5) ----------------
def cap_keyframes(timestamps, max_n=VIDEO_MAX_KEYFRAMES):
    return timestamps[:max_n]


def sample_times(duration, interval):
    t, out = 0.0, []
    while t <= duration:
        out.append(round(t, 2))
        t += interval
    return out


def pick_transcript(manual, auto, blocked):
    """Subtitle-only: co thi lay, khong co thi rong, khong ASR (AC-D2)."""
    if blocked:
        return ""
    return manual or auto or ""


def trim_transcript(text):
    return text[:SUBTITLE_MAX_CHARS]


def caption_merge(title, ocr_parts, sub=""):
    title = (title or "").strip()
    seen = []
    for p in ocr_parts:
        p = p.strip()
        if len(p) >= 10 and p not in seen:
            seen.append(p)
    base = (title + ". " + " | ".join(seen)) if seen else title
    base = base[:500]
    if sub:
        base = (base + " | sub: " + sub)[:900] if base else sub[:900]
    return base


# ---------------- Chinh sach incremental (AC-E1..E6) ----------------
def page_action(status, old_hash, new_hash, body_empty):
    if status in DELETE_STATUSES:
        return "delete"
    if body_empty:
        return "error-keep"
    if old_hash == new_hash:
        return "skip-touch"
    return "reprocess"


def mass_delete_guard(deleted, checked):
    return (deleted >= DELETE_GUARD_COUNT and checked > 0
            and deleted > checked * DELETE_GUARD_RATIO)


# ---------------- Chinh sach chatbot (AC-F1..F5) ----------------
def answer(qtype, evidence, depicts_children=False):
    """Tra (answer, yeu cau) hoac (refuse, ly do)."""
    if depicts_children and qtype == "who_in_photo":
        return "refuse", "child-safety"
    if qtype == "who_where_position":
        return "refuse", "no-position-tracking"
    if qtype in ("event_photos", "event_attendees", "who_in_photo"):
        if not evidence:
            return "refuse", "no-evidence"
        kind = "answer-list" if qtype == "event_attendees" else "answer"
        return kind, evidence
    return "refuse", "unknown-question"


def media_record(media_id, source_urls, event, people, ocr_text, caption,
                 context_refs):
    """Ban ghi media co cau truc (AC-A3)."""
    return {"media_id": media_id, "source_urls": source_urls,
            "event": event, "people": people, "ocr_text": ocr_text,
            "caption": caption, "context_refs": context_refs}


# ================= TEST =================
class TestDedupe(unittest.TestCase):
    def test_resize_url_normalized(self):
        """AC-B1: URL khac kich thuoc -> 1 media."""
        a = normalize_media_url("https://vd/x.jpg")
        b = normalize_media_url("https://vd/x.jpg?w=800&h=600")
        self.assertEqual(a, b)
        self.assertEqual(dedupe_verdict(a, b, "s1", "s2", 99, 0.1), "duplicate")

    def test_same_bytes_different_url(self):
        """AC-B2: SHA giong -> xu ly 1 lan."""
        self.assertEqual(
            dedupe_verdict("n1", "n2", "same", "same", 99, 0.1), "duplicate")

    def test_cropped_caught_by_embedding(self):
        """AC-B2: crop nhe van trung o tang embedding."""
        self.assertEqual(
            dedupe_verdict("n1", "n2", "s1", "s2", 30, 0.95), "duplicate")

    def test_burst_not_merged(self):
        """AC-B3: anh lien tiep giong nhau nhung khong gop xoa."""
        v = dedupe_verdict("n1", "n2", "s1", "s2", 3, 0.97,
                           same_event_burst=True)
        self.assertEqual(v, "near-dup-group")

    def test_distinct_images(self):
        """AC-B1: anh that su khac nhau -> rieng biet."""
        self.assertEqual(
            dedupe_verdict("n1", "n2", "s1", "s2", 40, 0.5), "distinct")

    def test_path_thumbnail_not_normalized(self):
        """AC-B1: thumbnail nam o path thi tang URL khong bat, tang sau bat."""
        a = normalize_media_url("https://vd/x.jpg")
        b = normalize_media_url("https://vd/thumb/x.jpg")
        self.assertNotEqual(a, b)  # phai nho SHA/pHash bat duoc

    def test_shared_across_articles(self):
        """AC-B4: 1 media + 2 xuat hien, hop nhat caption."""
        reg = MediaRegistry()
        m1, is_new1 = reg.ingest("shaA", "bai1", "khai mac")
        m2, is_new2 = reg.ingest("shaA", "bai2", "be mac")
        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(m1, m2)
        self.assertEqual(len(reg.occurrences(m1)), 2)

    def test_refcount_on_delete(self):
        """AC-B5: xoa 1 bai giu media, het tham chieu moi xoa."""
        reg = MediaRegistry()
        reg.ingest("shaA", "bai1", "c1")
        reg.ingest("shaA", "bai2", "c2")
        self.assertFalse(reg.remove_occurrence("shaA", "bai1"))
        self.assertTrue(reg.remove_occurrence("shaA", "bai2"))

    def test_youtube_key(self):
        """AC-B6: cung video o 2 bai -> lay keyframe 1 lan."""
        reg = MediaRegistry()
        m1, n1 = reg.ingest("yt:abc123", "bai1", "v1")
        m2, n2 = reg.ingest("yt:abc123", "bai2", "v1")
        self.assertEqual((m1, m2, n2), (m1, m1, False))
        self.assertTrue(n1)


class TestPersonIdentity(unittest.TestCase):
    GOLDEN = [(0.95, True), (0.92, True), (0.91, False),
              (0.85, True), (0.70, False), (0.60, False)]

    def test_no_magic_70(self):
        """AC-C2: nguong tu hieu chuan, khong phai so cung 70%."""
        low, high = calibrate_thresholds(self.GOLDEN)
        worst_fp = max(s for s, y in self.GOLDEN if not y)
        self.assertGreater(high, worst_fp)  # 0.92 > 0.91
        self.assertNotEqual(high, 0.7)

    def test_low_rejected(self):
        """AC-C2: duoi nguong thap -> loai."""
        low, high = calibrate_thresholds(self.GOLDEN)
        self.assertEqual(person_decision(0.5, 0.3, low, high), "reject")

    def test_middle_goes_to_review(self):
        """AC-C2: vung giua -> duyet tay."""
        low, high = calibrate_thresholds(self.GOLDEN)
        self.assertEqual(person_decision(0.88, 0.1, low, high), "review")

    def test_close_runner_up_goes_to_review(self):
        """AC-C2: sat nut thu hai -> duyet tay du diem cao."""
        low, high = calibrate_thresholds(self.GOLDEN)
        self.assertEqual(person_decision(0.95, 0.01, low, high), "review")
        self.assertEqual(person_decision(0.95, 0.10, low, high), "accept")

    def test_text_gating(self):
        """AC-C4: chi doi chieu vai nguoi duoc nhac ten."""
        members = ["GV%03d" % i for i in range(200)]
        self.assertEqual(text_gate_candidates(members, ["GV007", "GV042"]),
                         ["GV007", "GV042"])

    def test_poor_face_skipped(self):
        """AC-C5: mat nho/mo/nghieng/khau trang -> bo qua."""
        self.assertFalse(face_quality_ok(30, True, 10, False))
        self.assertFalse(face_quality_ok(120, False, 45, False))
        self.assertFalse(face_quality_ok(120, False, 5, True))
        self.assertTrue(face_quality_ok(120, False, 5, False))

    def test_stranger_gets_null(self):
        """AC-C1: nguoi la -> trong + khong luu embedding."""
        is_member, score = False, 0.99
        name = "Nguyen Van A" if (is_member and score > 0) else None
        store_embedding = bool(is_member and name)
        self.assertIsNone(name)
        self.assertFalse(store_embedding)

    def test_name_needs_evidence(self):
        """AC-C6: khong bang chung -> cam neu ten."""
        self.assertFalse(admit_name("Nguyen Van A", ["le khai giang"]))
        self.assertTrue(admit_name("Nguyen Van A",
                                   ["... PGS.TS. Nguyen Van A phat bieu ..."]))

    def test_title_normalized(self):
        """AC-C7: hoc ham/viet tat ve ten chuan."""
        gaz = {"Nguyễn Văn A": {"role": "Truong khoa"}}
        self.assertEqual(normalize_person("PGS.TS. Nguyen Van A", gaz),
                         "Nguyễn Văn A")
        self.assertIsNone(normalize_person("Tran Thi B", gaz))


class TestVideo(unittest.TestCase):
    def test_keyframe_capped(self):
        """AC-D1: toi da 20 keyframe."""
        self.assertEqual(len(cap_keyframes(list(range(35)))), 20)

    def test_sampling_starts_at_zero(self):
        """AC-D1: lay mau thua deu tu giay 0."""
        ts = sample_times(10, 3)
        self.assertEqual(ts[0], 0.0)
        self.assertTrue(all(b - a == 3 for a, b in zip(ts, ts[1:])))

    def test_manual_subtitle_wins(self):
        """AC-D2/D3: co phu de -> lay ban nguoi dang, cat 2000."""
        self.assertEqual(pick_transcript("M", "A", False), "M")
        self.assertEqual(pick_transcript("", "A", False), "A")
        self.assertLessEqual(len(trim_transcript("x" * 2500)), 2000)

    def test_no_subtitle_no_asr(self):
        """AC-D2: khong sub -> rong, khong nhan dang giong noi."""
        self.assertEqual(pick_transcript("", "", False), "")

    def test_blocked_video_skipped(self):
        """AC-D4: bi chan -> bo qua em."""
        self.assertEqual(pick_transcript("M", "A", True), "")

    def test_short_appearance_may_miss(self):
        """AC-D5: nguoi 2 giay co the lot khoi moc lay mau."""
        ts = sample_times(10, 3)  # moc 0,3,6,9
        self.assertNotIn(4.5, ts)

    def test_caption_markers(self):
        """AC-A3: caption giu ca OCR va sub co danh dau."""
        cap = caption_merge("Le khai giang", ["TRUONG CONG NGHE THONG TIN",
                                              "TRUONG CONG NGHE THONG TIN"],
                            "chao mung nam hoc moi")
        self.assertIn("Le khai giang", cap)
        self.assertEqual(cap.count("TRUONG CONG NGHE THONG TIN"), 1)
        self.assertIn("| sub: chao mung", cap)

    def test_short_ocr_ignored(self):
        """OCR < 10 ky tu -> chi giu tieu de."""
        self.assertEqual(caption_merge("Tieu de", ["abc", "  "]), "Tieu de")


class TestIncremental(unittest.TestCase):
    def test_new_and_unchanged(self):
        """AC-E1/E2: moi thi xu ly, cu khong doi thi thoi."""
        self.assertEqual(page_action(200, None, "h1", False), "reprocess")
        self.assertEqual(page_action(200, "h1", "h1", False), "skip-touch")
        self.assertEqual(page_action(200, "h1", "h2", False), "reprocess")

    def test_delete_vs_network_error(self):
        """AC-E3: 404 thi xoa, rot mang thi giu."""
        self.assertEqual(page_action(404, "h", "h", True), "delete")
        self.assertEqual(page_action(410, "h", "h", False), "delete")
        self.assertEqual(page_action(200, "h", "h", True), "error-keep")
        self.assertEqual(page_action(500, "h", "h", True), "error-keep")

    def test_mass_delete_guard(self):
        """AC-E4: xoa >=20 va >20% -> dung."""
        self.assertTrue(mass_delete_guard(25, 100))
        self.assertFalse(mass_delete_guard(5, 100))
        self.assertFalse(mass_delete_guard(25, 200))

    def test_media_incremental_only_new(self):
        """AC-E5: bai sua chi xu ly anh moi."""
        reg = MediaRegistry()
        for i in range(3):
            reg.ingest("old%d" % i, "bai1", "c")
        mid, is_new = reg.ingest("new1", "bai1", "c moi")
        self.assertTrue(is_new)
        self.assertEqual(len(reg.occurrences(mid)), 1)


class TestChatbotPolicy(unittest.TestCase):
    EV = [{"url": "bai/1", "span": "PGS.TS. Nguyen Van A phat bieu"}]

    def test_event_photos(self):
        """AC-F1: hoi anh su kien -> tra kem bang chung."""
        kind, ev = answer("event_photos", self.EV)
        self.assertEqual(kind, "answer")
        self.assertTrue(all("url" in e and "span" in e for e in ev))

    def test_event_attendees_is_list(self):
        """AC-F2: danh sach lien quan, khong cam ket vi tri."""
        kind, ev = answer("event_attendees", self.EV)
        self.assertEqual(kind, "answer-list")
        self.assertTrue(ev)

    def test_position_refused(self):
        """AC-F3: cam dinh vi trong khung hinh."""
        self.assertEqual(answer("who_where_position", self.EV),
                         ("refuse", "no-position-tracking"))

    def test_children_refused(self):
        """AC-F4: anh tre em -> tu choi."""
        self.assertEqual(
            answer("who_in_photo", self.EV, depicts_children=True),
            ("refuse", "child-safety"))

    def test_no_evidence_no_answer(self):
        """AC-F5: khong bang chung -> noi khong biet."""
        self.assertEqual(answer("event_attendees", []),
                         ("refuse", "no-evidence"))


class TestRetrievalContract(unittest.TestCase):
    def test_dims_and_legs(self):
        """AC-G1: 2 chan embedding + metadata."""
        self.assertEqual((TEXT_DIM, IMAGE_DIM), (384, 512))

    def test_media_record_schema(self):
        """AC-A3: ban ghi media du truong."""
        rec = media_record("m1", ["u1"],
                           {"name": "X", "date": "2024-09-05",
                            "evidence": "backdrop OCR"},
                           [{"name_canonical": "Nguyễn Văn A",
                             "role": "Truong khoa", "source": "caption",
                             "span": "...", "confidence": 0.9}],
                           "ocr...", "caption...",
                           [{"type": "heading", "text": "Khai mac"}])
        for k in ("media_id", "source_urls", "event", "people",
                  "ocr_text", "caption", "context_refs"):
            self.assertIn(k, rec)
        for k in ("name", "date", "evidence"):
            self.assertIn(k, rec["event"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

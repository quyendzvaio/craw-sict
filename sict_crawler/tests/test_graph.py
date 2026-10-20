"""Graph thực thể D20: trích người/sự kiện/CLB, danh bạ + khớp câu hỏi, cạnh ảnh↔thực thể,
router chấp nhận ứng viên graph, parser lấy đoạn văn sát ảnh.

Run: pytest sict_crawler/tests/test_graph.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler import graph as G
from sict_crawler import parser, router

FIX = Path(__file__).parent / "fixtures"


def _names(ms, kind):
    return [m.name for m in ms if m.kind == kind]


def test_extract_persons_prefix_bare_and_noise():
    t = ("PGS.TS. Nguyễn Thị Kim Sơn và TS. Đặng Trọng Hợp phát biểu. Sinh viên Nguyễn Việt Hoàng "
         "nhận giải. Trường ĐH Công nghiệp Hà Nội, CN Dệt May, TP. Hồ Chí Minh, Thái Bình.")
    ps = _names(G.extract_persons(t), "person")
    assert {"Nguyễn Thị Kim Sơn", "Đặng Trọng Hợp", "Nguyễn Việt Hoàng"} <= set(ps)
    assert not any(n in ps for n in ("Dệt May", "Hồ Chí Minh", "Thái Bình", "Hà Nội"))
    # tên riêng trùng địa danh vẫn là tên người khi đứng cuối tên đầy đủ
    assert "Nguyễn Thị Mỹ Bình" in _names(G.extract_persons("ThS. Nguyễn Thị Mỹ Bình"), "person")


def test_extract_events_and_orgs():
    ev = {m.key for m in G.extract_events("Sôi động `IT FESTIVAL 2016` tại SICT")}
    assert "it festival" in ev                          # năm bị bỏ khỏi khoá → 1 chuỗi sự kiện
    ev = {m.key for m in G.extract_events("Lễ tốt nghiệp năm 2025 – dấu ấn thanh xuân")}
    assert "le tot nghiep" in ev
    assert not G.extract_events("Hội thảo khoa học")   # cụm quá chung
    orgs = G.extract_orgs("Câu lạc bộ Tin học HIT tổ chức sinh nhật. CLB HIT có 14 tuổi.")
    assert {m.key for m in orgs} == {"clb hit"}         # gộp theo tên viết tắt


def _gaz():
    docs = [
        {"doc_id": "a", "title": "TS. Đặng Trọng Hợp", "doc_type": "page", "section": "can-bo-giang-vien"},
        {"doc_id": "b", "title": "Gala tổng kết IT FESTIVAL 2025", "doc_type": "article", "section": "tin-tuc"},
        {"doc_id": "c", "title": "Sôi động `IT Festival 2016`", "doc_type": "article", "section": "tin-tuc"},
        {"doc_id": "d", "title": "Chúc mừng sinh nhật Câu lạc bộ Tin học HIT", "doc_type": "article",
         "section": "tin-tuc"},
        {"doc_id": "e", "title": "Các sản phẩm báo tường trong ngày hội công nghệ thông tin IT Festival 2023",
         "doc_type": "article", "section": "thu-vien-anh"},
    ]
    ms = [G.doc_mentions(d, "ThS. Nguyễn Thị Cẩm Ngoan hướng dẫn." if d["doc_id"] == "b" else "")
          for d in docs]
    return docs, G.build_gazetteer(ms)


def test_gazetteer_match_questions():
    _, gaz = _gaz()
    kinds = lambda q: [(e.kind, e.key) for e in gaz.match(q, query=True)]  # noqa: E731  câu hỏi người dùng
    hop, ngoan = G.person_key("Đặng Trọng Hợp"), G.person_key("Nguyễn Thị Cẩm Ngoan")
    assert ("person", hop) in kinds("Cho xem ảnh thầy Đặng Trọng Hợp")
    assert ("person", hop) in kinds("anh thay dang trong hop")   # không dấu
    assert ("person", ngoan) in kinds("ảnh cô Cẩm Ngoan")         # tên đệm + tên
    assert ("event", "it festival") in kinds("Hình ảnh IT Festival 2018")
    assert ("org", "clb hit") in kinds("CLB HIT có hoạt động gì?")
    # chồng lấn: 1 tiêu đề nêu 2 cách gọi cùng sự kiện
    assert ("event", "it festival") in kinds("ngày hội công nghệ thông tin IT Festival")
    assert kinds("Học phí ngành công nghệ thông tin?") == []


def test_link_document_strong_weak():
    docs, gaz = _gaz()
    prof = docs[0]
    g = G.link_document(prof, "", [G.MediaRef("s1", "image", "TS. Đặng Trọng Hợp", "", "")], gaz)
    # trang hồ sơ: alt trùng tiêu đề vẫn là ảnh của chính người đó (mạnh)
    hop = G.person_key("Đặng Trọng Hợp")
    assert [(e.key, w) for e, w, _ in g.depicts["s1"]] == [(hop, G.W_DEPICT_PROFILE)]
    assert g.media_keys("s1", strong_only=True) == [f"person:{hop}"]
    ev = docs[1]
    g = G.link_document(ev, "", [G.MediaRef("s2", "image", ev["title"], "", ""),
                                 G.MediaRef("s3", "image", "", "ThS. Nguyễn Thị Cẩm Ngoan trao giải", "")],
                        gaz)
    assert g.media_keys("s2") == ["event:it festival"]            # yếu: ảnh thuộc bài về sự kiện
    assert g.media_keys("s2", strong_only=True) == []
    ngoan = G.person_key("Nguyễn Thị Cẩm Ngoan")
    assert f"person:{ngoan}" in g.media_keys("s3", strong_only=True)  # mạnh: đoạn sát ảnh
    rows = G.link_rows(g, 7, {"s2": 1, "s3": 2}, {}, {("event", "it festival"): 11,
                                                       ("person", ngoan): 12})
    assert {(r["entity_id"], r["rel"], r["media_id"]) for r in rows} >= {
        (11, "about", None), (11, "depicts", 1), (12, "depicts", 2)}
    ev = {(r["entity_id"], r["media_id"]): r["evidence"] for r in rows}
    assert "Cẩm Ngoan trao giải" in ev[(12, 2)]          # AC-C6: tên kèm đoạn trích bằng chứng


def test_router_gate_graph_modality():
    text = [{"document_id": 1}]
    cands = [{"media_key": "g", "kind": "image", "document_id": [5], "modality": "graph",
              "raw_score": 1.0, "score": 0.2},
             {"media_key": "w", "kind": "image", "document_id": [5], "modality": "graph",
              "raw_score": 0.5, "score": 0.3},
             {"media_key": "c", "kind": "image", "document_id": [1], "modality": "clip",
              "raw_score": 0.3, "score": 0.3}]
    out = router.gate(router.Route("required"), text, cands, top_k=5, clip_min=0.24,
                      caption_min=0.82, graph_min=0.6)
    keys = [h["media_key"] for h in out]
    assert "w" not in keys and set(keys) == {"g", "c"}   # graph dưới ngưỡng cạnh bị loại


def test_router_accented_anh_is_image_intent():
    assert router.classify_rules("Ảnh PGS.TS Nguyễn Thị Kim Sơn").media == "required"
    assert router.classify_rules("Ảnh hưởng của AI tới việc làm?").media == "none"
    assert router.classify_rules("anh Nguyễn Việt Hoàng là ai").media == "none"


def test_parser_image_context():
    html = (FIX / "sict_article.html").read_text(encoding="utf-8")
    p = parser.parse_html("https://sict.haui.edu.vn/vn/tin-tuc/x/1", html)
    assert len(p.image_contexts) == len(p.image_urls)
    t = """<html><body><div class="irs-blog-single-col"><h2>Tiêu đề bài</h2>
    <table><tr><td><img src="/a.jpg" alt="x"></td><td>ThS. Nguyễn Thị Cẩm Ngoan</td></tr></table>
    <p>Đoạn mở đầu về sự kiện.</p><p><img src="/b.jpg"></p><p>TS. Đặng Trọng Hợp phát biểu.</p>
    </div></body></html>"""
    p = parser.parse_html("https://sict.haui.edu.vn/vn/tin-tuc/y/2", t)
    ctx = dict(zip([u.rsplit("/", 1)[-1] for u in p.image_urls], p.image_contexts))
    assert "Cẩm Ngoan" in ctx["a.jpg"]                   # ô bảng cùng hàng
    assert "Đặng Trọng Hợp" in ctx["b.jpg"] and "mở đầu" in ctx["b.jpg"]   # đoạn trước + sau


def test_relations_from_links():
    L = lambda e, d, rel, w=1.0, m=None: {"entity_id": e, "document_id": d, "media_id": m,  # noqa: E731
                                           "video_id": None, "rel": rel, "weight": w}
    kinds = {1: "person", 2: "event", 3: "person", 4: "org"} | {100 + i: "person" for i in range(200)}
    links = [
        # bài 10: tiêu đề nêu sự kiện 2; ảnh 7 nêu cả người 1 và 3 (đoạn sát ảnh)
        L(2, 10, "about"), L(1, 10, "depicts", 1.0, 7), L(3, 10, "depicts", 1.0, 7),
        # bài 11: tiêu đề nêu người 1 + sự kiện 2
        L(1, 11, "about"), L(2, 11, "about"),
        # bài 12: file đính kèm, 200 tên chỉ trong thân bài → KHÔNG ghép cặp
        *[L(100 + i, 12, "mentions", 0.5) for i in range(200)],
        # bài 13: ảnh yếu (bài về CLB) không tính là cùng ảnh
        L(4, 13, "about"), L(3, 13, "depicts", 0.6, 9),
    ]
    rows = {(r["src_id"], r["dst_id"]): r for r in G.relations_from_links(links, kinds)}
    assert rows[(1, 3)]["weight"] == 0.5 and rows[(1, 3)]["media_count"] == 1   # 1 bài: giảm chấn ×0,5
    assert rows[(1, 3)]["rel"] == "person-person"
    r12 = rows[(1, 2)]                      # bài 10 (0.5: tiêu đề + ảnh) và bài 11 (0.8: cùng tiêu đề)
    assert r12["doc_count"] == 2 and r12["rel"] == "person-event"
    assert abs(r12["weight"] - (1 - (1 - 0.5 * 0.5) * (1 - 0.5 * 0.8))) < 1e-6 and r12["doc_ids"][0] == 11
    assert not any(a >= 100 and b >= 100 for a, b in rows)          # danh sách SV không sinh cặp
    assert (3, 4) not in rows                                       # depicts yếu không phải bằng chứng


def test_graph_facts_for_llm():
    a = G.Entity("person", "dang trong hop", "Đặng Trọng Hợp", id=1)
    b = G.Entity("event", "it festival", "IT Festival", id=2)
    c = G.Entity("org", "clb hit", "CLB Tin học HIT", id=3)
    gaz = G.Gazetteer([a, b, c])
    G.set_relations([(1, 2, 0.9, 5, 1), (1, 3, 1.0, 1, 1)], gaz)
    try:
        assert [r.entity.name for r in G.related(a)] == ["IT Festival", "CLB Tin học HIT"]  # nhiều bài trước
        f = G.facts([a])
        assert f == ["Đặng Trọng Hợp (người) liên quan — sự kiện: IT Festival (5 bài) | "
                     "CLB/tổ chức: CLB Tin học HIT (1 bài)"]
        assert G.related(b, kind="person")[0].entity is a
    finally:
        G.set_relations([], None)


def test_person_keys_keep_diacritics():
    """Bỏ dấu gộp nhầm người khác nhau: Hùng ≠ Hưng; vị trí dấu cũ/mới vẫn là 1 người."""
    assert G.person_key("Lê Thị Thủy") == G.person_key("Lê Thị Thuỷ")
    assert G.person_key("Nguyễn Văn Hùng") != G.person_key("Nguyễn Văn Hưng")
    hung = G.Entity("person", G.person_key("Nguyễn Văn Hùng"), "Nguyễn Văn Hùng", doc_count=4)
    hung2 = G.Entity("person", G.person_key("Nguyễn Văn Hưng"), "Nguyễn Văn Hưng", doc_count=5)
    gaz = G.Gazetteer([hung, hung2])
    assert gaz.match("ảnh thầy Nguyễn Văn Hưng") == [hung2]      # có dấu → đúng người
    assert gaz.match("anh thay nguyen van hung", query=True) == []            # không dấu, 2 người ngang nhau → bỏ
    gaz = G.Gazetteer([hung, G.Entity("person", hung2.key, "Nguyễn Văn Hưng", doc_count=20)])
    assert gaz.match("nguyen van hung", query=True)[0].name == "Nguyễn Văn Hưng"  # 1 người chiếm áp đảo
    # tên gần giống chỉ vào hàng chờ, không tự gộp
    anh = G.Entity("person", G.person_key("Lê Thị Ánh"), "Lê Thị Ánh", doc_count=1)
    anh0 = G.Entity("person", G.person_key("Lê Thị Anh"), "Lê Thị Anh", doc_count=44)
    assert G.near_duplicates(anh, G.near_dup_index([anh0])) == [anh0]


def test_article_text_requires_capitalised_person_names():
    """Văn bản bài viết (query=False): "cao đẳng khóa 21" / địa chỉ viết thường không thành người; "tên đệm
    + tên" viết hoa giữa tên tổ chức ("Tập đoàn Hồng Hải") chỉ nhận sau thầy/cô/TS…"""
    hai = G.Entity("person", G.person_key("Hà Hồng Hải"), "Hà Hồng Hải", doc_count=3)
    cdk = G.Entity("person", G.person_key("Cao Đẳng Khóa"), "Cao Đẳng Khóa", doc_count=2)
    gaz = G.Gazetteer([hai, cdk])
    assert gaz.match("sinh viên trình độ cao đẳng khóa 21") == []
    assert gaz.match("Tập đoàn Khoa học kỹ thuật Hồng Hải tuyển dụng") == []
    assert gaz.match("cảm ơn thầy Hồng Hải đã hướng dẫn") == [hai]
    assert gaz.match("TS. Hà Hồng Hải phát biểu") == [hai]
    assert gaz.match("tập đoàn hồng hải", query=True) == [hai]          # câu hỏi người dùng: vẫn nhận


def test_runon_merge_only_for_titles_not_names():
    """Gộp tên chạy lấn chỉ khi phần thừa là chức danh / họ tên kế tiếp — "Nguyễn Thị Kim" + "Anh" là
    người khác, không được gộp."""
    pk = G.person_key
    assert G.runon_tail(pk("Đặng Trọng Hợp Hiệu"), pk("Đặng Trọng Hợp"))
    assert G.runon_tail(pk("Chu Thị Quyên Chu Thị"), pk("Chu Thị Quyên"))
    assert not G.runon_tail(pk("Nguyễn Thị Kim Anh"), pk("Nguyễn Thị Kim"))
    ms = [[G.Mention("person", pk("Nguyễn Thị Kim"), "Nguyễn Thị Kim")] for _ in range(3)]
    ms += [[G.Mention("person", pk("Nguyễn Thị Kim Anh"), "Nguyễn Thị Kim Anh")]]
    gaz = G.build_gazetteer(ms)
    assert ("person", pk("Nguyễn Thị Kim Anh")) in gaz.entities
    assert [e.name for e in gaz.match("TS. Nguyễn Thị Kim Anh")] == ["Nguyễn Thị Kim Anh"]
    assert [m.name for m in G.extract_persons("TS. Nguyễn Văn Tỉnh – Phó Trưởng phòng")] == ["Nguyễn Văn Tỉnh"]


def test_review_pair_suggestions_are_conservative():
    from sict_crawler.graphsync import suggest_pair
    P = lambda n, c: G.Entity("person", G.person_key(n), n, doc_count=c)  # noqa: E731
    assert suggest_pair(P("Nguyễn Mạnh Cương", 1), P("Nguyễn Mạnh Cường", 94))[0] == "merged?"   # gõ sai dấu
    assert suggest_pair(P("Nguyễn Văn Hùng", 9), P("Nguyễn Văn Hưng", 9))[0] == "uncertain"    # 2 người?
    assert suggest_pair(P("Nguyễn Thanh Huyền", 2), P("Nguyễn Thị Thanh Huyền", 42))[0] == "merged?"
    assert suggest_pair(P("Nguyễn Bá Việt Anh", 1), P("Nguyễn Việt Anh", 10))[0] == "uncertain"
    assert suggest_pair(P("Nguyễn Thị Nga", 4), P("Nguyễn Thị Hằng", 4))[0] == "distinct"


def test_places_and_rare_surnames_and_staff_profile():
    names = lambda t: [m.name for m in G.extract_persons(t)]  # noqa: E731
    assert names("trụ sở tại Phường Lê Hồng Phong, gần THPT Lê Quý Đôn và Đền thờ Chu Văn An") == []
    assert names("Hiệu trưởng Nguyễn Văn Bình phát biểu") == ["Nguyễn Văn Bình"]     # "trưởng" ≠ "trường"
    assert names("1 | Nguyễn Văn An | CNTT01") == ["Nguyễn Văn An"]                  # cột STT không phải số nhà
    assert names("ThS. Đoàn Văn Trung và Đoàn Thanh Niên Khoa") == ["Đoàn Văn Trung"]
    assert names("Thạc sĩ An Văn Minh, sinh viên Lù Thị Thu Hằng") == ["An Văn Minh", "Lù Thị Thu Hằng"]
    p = G.Entity("person", G.person_key("Lê Hồng Phong"), "Lê Hồng Phong", doc_count=2)
    assert G.Gazetteer([p]).match("Phòng 103, Phường Lê Hồng Phong, Hà Nội") == []
    assert G.Gazetteer([p]).match("thầy Lê Hồng Phong") == [p]
    prof = lambda t, slug: G.staff_from_profile({"section": "can-bo-giang-vien", "title": t,  # noqa: E731
                                                 "url": f"https://sict.haui.edu.vn/vn/html/can-bo-giang-vien/{slug}"})
    assert prof("ThS. Vũ Thị Tuyết MaI", "ths-vu-thi-tuyet-mai").name == "Vũ Thị Tuyết Mai"
    assert prof("​​TS. Phạm Huy Thông", "ts-pham-huy-thong").name == "Phạm Huy Thông"
    assert prof("Ths. Nguyễn Tuấn Anh", "ths-nguyen-tuan-anh1").attrs["staff"] is True
    assert prof("ThS. Nguyễn Xuân Hoàng", "ths-nguyen-xuan-hoa").name == "Nguyễn Xuân Hoàng"  # slug bị cắt
    assert prof("Thạc sĩ An Văn Minh", "ths-an-van-minh").name == "An Văn Minh"
    assert prof("Giới thiệu đội ngũ", "ths-an-van-minh") is None                          # tiêu đề không khớp slug


def _cat():
    return G.Catalog([
        G.Entity("org", "clb hit", "CLB Tin học HIT", ["clb tin hoc", "clb tin hoc hit"], 0,
                 attrs={"catalog": True, "acronyms": ["HIT"]}),
        G.Entity("event", "thu thach lap nghiep", "Cuộc thi Thử thách Lập nghiệp", [], 0, attrs={"catalog": True}),
        G.Entity("event", "olympic tin hoc", "Olympic Tin học (OLP)", [], 0,
                 attrs={"catalog": True, "acronyms": ["OLP"]}),
    ], {("event", "sinh vien 5 tot")})


def test_catalog_canonicalises_variants_and_blocks():
    cat = _cat()
    ms = [G.Mention("event", G.event_key(n), n) for n in (
        "Chung kết Cuộc thi Thử thách Lập nghiệp lần thứ VII", "Cuộc thi Olympic Tin học sinh viên Việt Nam",
        "Sinh viên 5 tốt", "Hội thảo Data Analyst")] + [G.Mention("org", "clb tin hoc", "CLB Tin học")]
    out = [cat.canon(m) for m in ms]
    assert [m.key if m else None for m in out] == ["thu thach lap nghiep", "olympic tin hoc", None,
                                                   "hoi thao data analyst", "clb hit"]
    gaz = G.build_gazetteer([ms[:2], ms[2:]], cat)
    assert gaz.lookup("org", "clb hit").name == "CLB Tin học HIT"          # tên chuẩn, có mặt dù luật không trích
    assert ("event", "sinh vien 5 tot") not in gaz.entities


def test_catalog_acronyms_are_case_sensitive_in_articles():
    gaz = G.build_gazetteer([], _cat())
    keys = lambda t, q=False: [e.key for e in gaz.match(t, query=q)]  # noqa: E731
    assert keys("Thành viên CLB HIT và đội OLP của trường") == ["clb hit", "olympic tin hoc"]
    assert keys("thầy hit một cú, giải olp") == []                          # chữ thường trong bài: không phải viết tắt
    assert keys("clb hit có gì", q=True) == ["clb hit"]                    # câu hỏi người dùng gõ thường: vẫn nhận
    assert keys("đội HIT_Titans") == []                                     # tên đội ≠ CLB


def test_event_key_strips_round_prefix_and_truncation_dropped():
    assert G.event_key("Chung kết Cuộc thi DENSO Factory Hacks 2024") == "denso factory hacks"
    assert G.event_key("Cuộc thi Nghiên cứu khoa học") == "cuoc thi nghien cuu khoa hoc"   # cụm chung: giữ tiền tố
    assert G.event_key("Chung kết cuộc thi tấm thiệp tri ân") == "chung ket cuoc thi tam thiep tri an"
    assert [m.name for m in G.extract_persons("Số 298 Đường Cầu Diễn, Bắc Từ Liêm")] == []
    assert G.event_key("Cuộc thi AI") == "cuoc thi ai"                      # còn < 2 từ → giữ nguyên
    names = [m.name for m in G.extract_events("Lễ ký kết Biên bản ghi nhớ hợp tác giữa Trường CNTT và Viettel")]
    assert names == [] or not any(G.fold(n).split()[-1] == "giua" for n in names)
    assert [m.name for m in G.extract_events('Nam sinh viên "đa năng" và “Thiếu gia ngược đạo”')] == ["Thiếu gia ngược đạo"]


def test_contained_event_variants_merge():
    a = G.Mention("event", G.event_key("Sinh viên với ý tưởng khởi nghiệp"), "Sinh viên với ý tưởng khởi nghiệp")
    b = G.Mention("event", G.event_key("Học sinh, sinh viên với ý tưởng khởi nghiệp"), "Học sinh, sinh viên…")
    gaz = G.build_gazetteer([[a], [a], [b]], G.Catalog([], set()))
    assert len([e for e in gaz.entities.values() if e.kind == "event"]) == 1


def test_relations_damped_and_excluded():
    links = [{"entity_id": 1, "document_id": 9, "media_id": 5, "video_id": None, "rel": "depicts", "weight": 1.0},
             {"entity_id": 2, "document_id": 9, "media_id": 5, "video_id": None, "rel": "depicts", "weight": 1.0},
             {"entity_id": 3, "document_id": 9, "media_id": 5, "video_id": None, "rel": "depicts", "weight": 1.0}]
    kinds = {1: "person", 2: "person", 3: "event"}
    rows = G.relations_from_links(links, kinds, exclude={3})
    assert [(r["src_id"], r["dst_id"], r["weight"]) for r in rows] == [(1, 2, 0.5)]   # 1 ảnh chung: 0,5, không 1,0
    two = links[:2] + [{**x, "document_id": 10, "media_id": 6} for x in links[:2]]
    assert G.relations_from_links(two, kinds)[0]["weight"] == 0.75                   # 2 bài làm chứng


def test_street_names_and_cao_dang_khoa():
    names = lambda t: [m.name for m in G.extract_persons(t)]  # noqa: E731
    assert names("Tầng 1, 643 Phạm Văn Đồng, Bắc Từ Liêm và 128A Hồ Tùng Mậu") == []
    assert names("Lịch thi Hệ Cao Đẳng Khóa 21") == []
    p = G.Entity("person", G.person_key("Lê Hồng Phong"), "Lê Hồng Phong", doc_count=2)
    assert G.Gazetteer([p]).match("Cơ sở 3: Lê Hồng Phong, Phủ Lý") == []
    assert G.Gazetteer([p]).match("cảm ơn thầy Lê Hồng Phong") == [p]

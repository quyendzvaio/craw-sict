"""Bộ dựng markdown của khung chat (sict_crawler/api/ui/md.js), chạy bằng node. Máy không có node → bỏ qua.

2026-11-04: UI gán câu trả lời bằng textContent nên **đậm**, danh sách, > hiện nguyên ký tự."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MD_JS = Path(__file__).resolve().parents[1] / "sict_crawler" / "api" / "ui" / "md.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="cần node")


def render(md: str, cites: dict | None = None) -> str:
    js = (f"const {{renderMarkdown}} = require({json.dumps(str(MD_JS))});"
          f"process.stdout.write(renderMarkdown({json.dumps(md)}, {json.dumps(cites or {})}));")
    return subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout


def test_escapes_html_and_rejects_non_http_links():
    out = render('<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) **<b>**')
    assert "<script" not in out and "<img" not in out and "<b>" not in out
    assert "&lt;script&gt;" in out and 'href="javascript' not in out
    assert "<strong>&lt;b&gt;</strong>" in out


def test_answer_from_screenshot():
    md = ("**Ngành CNTT**\n\n- **Mô tả chung:** Đào tạo [4]\n- **Triển vọng:**\n"
          "  - *3-5 năm:* chuyên viên.\n  - *Trên 10 năm:* CTO [3]\n- **Điểm chuẩn 2025:** 25.22 [3]\n\n"
          "> **Lưu ý:** Ngữ cảnh không có chi tiết.")
    out = render(md, {"3": {"n": 3, "url": "https://sict.haui.edu.vn/a?x=1&y=2", "cite": "CTĐT"}})
    assert "**" not in out and "&gt;" not in out
    assert out.startswith("<p><strong>Ngành CNTT</strong></p><ul><li><strong>Mô tả chung:</strong>")
    assert "<ul><li><em>3-5 năm:</em> chuyên viên.</li><li><em>Trên 10 năm:</em>" in out   # lồng trong mục cha
    assert '<a class="cite" href="https://sict.haui.edu.vn/a?x=1&amp;y=2"' in out and '<span class="cite">[4]</span>' in out
    assert "<blockquote><p><strong>Lưu ý:</strong>" in out


def test_tables_ordered_lists_code_and_urls():
    out = render("| Ngành | Điểm |\n|---|:-:|\n| CNTT | 25.22 |\n\n3. ba\n4. bốn\n\n```\na < b\n```\n"
                 "Xem https://sict.haui.edu.vn/vn/x. và `mã`")
    assert "<table><thead><tr><th>Ngành</th><th>Điểm</th></tr></thead><tbody><tr><td>CNTT</td><td>25.22</td>" in out
    assert '<ol start="3"><li>ba</li><li>bốn</li></ol>' in out
    assert "<pre><code>a &lt; b</code></pre>" in out
    assert '<a href="https://sict.haui.edu.vn/vn/x" target="_blank" rel="noopener">https://sict.haui.edu.vn/vn/x</a>.' in out
    assert "<code>mã</code>" in out


def test_plain_text_keeps_line_breaks_and_math_stars():
    out = render("Dòng 1\nDòng 2\n\n2*3*4 = 24 và snake_case_name")
    assert out == "<p>Dòng 1<br>Dòng 2</p><p>2*3*4 = 24 và snake_case_name</p>"

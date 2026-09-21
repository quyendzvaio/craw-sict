"""Self-check for parser + chunker (no external services needed).

Run: python -m sict_crawler.tests.test_parser
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sict_crawler.chunker import chunk_text
from sict_crawler.frontier import canonicalize
from sict_crawler.parser import parse_html

HTML = """
<html><head><title>Ngành An toàn thông tin</title></head><body>
<nav><a href='/vn'>Home</a></nav>
<main>
<h1>Giới thiệu ngành An toàn thông tin</h1>
<p>Chương trình đào tạo kỹ sư An toàn thông tin trong 4 năm, 140 tín chỉ.</p>
<table><tr><th>Học kỳ</th><th>Số tín chỉ</th></tr><tr><td>1</td><td>16</td></tr><tr><td>2</td><td>18</td></tr></table>
<img src='/media/2/atpt.jpg'>
<video src='/media/intro.mp4'></video>
<iframe src='https://www.youtube.com/embed/abc123'></iframe>
<a href='/vn/tin-tuc/quy-che/123'>Quy chế</a>
<a href='https://www.haui.edu.vn'>HaUI</a>
</main>
<footer>Copyright</footer>
</body></html>
"""


def test():
    p = parse_html("https://sict.haui.edu.vn/vn/html/an-toan-thong-tin", HTML)
    assert "An toàn thông tin" in p.title
    assert "140 tín chỉ" in p.markdown
    assert len(p.tables) == 1 and p.tables[0][0] == ["Học kỳ", "Số tín chỉ"]
    assert any("atpt.jpg" in u for u in p.image_urls)
    assert any("intro.mp4" in u for u in p.video_urls)
    assert any("youtube" in u for u in p.video_urls)
    assert any("/vn/tin-tuc/quy-che/123" in l for l in p.links)
    assert not any("no-image" in u for u in p.image_urls)

    chunks = chunk_text(p.markdown, p.title, min_tokens=10, max_tokens=50)
    assert len(chunks) >= 1
    assert all(c.text for c in chunks)

    assert canonicalize("https://SICT.HAUI.EDU.VN/vn/x?b=2&a=1&utm_source=fb#frag") == \
        "https://sict.haui.edu.vn/vn/x?a=1&b=2"
    print("OK — parser, chunker, canonicalize")


if __name__ == "__main__":
    test()

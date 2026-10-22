"""Self-check for keyframe dedup + timestamp parsing (no services needed).

Run: python -m tests.test_video
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sict_crawler.service.index.video import _pts_times, dedup_indices, extract_keyframes


def norm(v: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in v))
    return [x / s for x in v]


def test_pts_times():
    stderr = ("[Parsed_showinfo_1 @ 0x...] n:   0 pts:180 pts_time:2\n"
              "[Parsed_showinfo_1 @ 0x...] n:   1 pts:1106 pts_time:12.345\n")
    assert _pts_times(stderr) == [2.0, 12.345], _pts_times(stderr)


def test_dedup():
    v0 = norm([1.0, 0.0])
    v1 = [0.99, 0.141]  # near-dup of v0 (cos ≈ 0.99) — must be dropped
    v2 = norm([0.0, 1.0])  # orthogonal — must be kept
    kept = dedup_indices([v0, v1, v2], threshold=0.8)
    assert kept == [0, 2], kept


def test_dedup_all_unique():
    vecs = [norm([1, 0]), norm([0, 1]), norm([1, 1])]
    assert dedup_indices(vecs, threshold=0.8) == [0, 1, 2]


def test_extract_keyframes_lavfi():
    """ffmpeg smoke: synthetic 12s testsrc → 0s + every 5s = [0, 5, 10]."""
    if not shutil.which("ffmpeg"):
        print("skip: no ffmpeg")
        return
    tmp = tempfile.mkdtemp(prefix="sict_test_")
    src = f"{tmp}/t.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=12",
                    "-vf", "fps=10", src], capture_output=True, check=True)
    frames = extract_keyframes(src, max_frames=10, interval=5.0)  # tường minh: default giờ là 10s
    assert frames, "no keyframes extracted"
    ts = [t for _, t in frames]
    assert ts == [0.0, 5.0, 10.0], ts


if __name__ == "__main__":
    test_pts_times()
    print("OK — pts_times")
    test_dedup()
    test_dedup_all_unique()
    print("OK — dedup_indices")
    test_extract_keyframes_lavfi()
    print("OK — extract_keyframes (ffmpeg smoke)")

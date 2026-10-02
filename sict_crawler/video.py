"""Video → keyframes via ffmpeg + subtitle-only transcript (no ASR).

Subtitle: chỉ lấy caption/subtitle có sẵn của YouTube (hoặc host yt-dlp
hỗ trợ). Không có → trả "" và caller bỏ qua luôn, KHÔNG chạy Whisper.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

import httpx

from .config import get_settings

MIN_TS_GAP = 0.5  # seconds — dedup/top-up frames must differ by at least this

# ponytail: yt-dlp lazily imported; only embed hosts resolved — direct .mp4 skips it
_EMBED_RE = re.compile(r"(youtube\.com|youtu\.be|vimeo\.com|facebook\.com)", re.I)

SUBTITLE_LANGS = ("vi", "en")
SUBTITLE_MAX_CHARS = 2000


def fetch_subtitles(source: str, langs: tuple[str, ...] = SUBTITLE_LANGS) -> str:
    """Lấy subtitle có sẵn (manual trước, auto sau). Không có → "".

    Chỉ resolve embed URL (youtube/vimeo/facebook). File .mp4 trực tiếp
    không có subtitle → "" luôn. Không tải video, chỉ fetch file vtt/json.
    """
    if not _EMBED_RE.search(source):
        return ""
    try:
        import yt_dlp
    except ImportError:
        return ""
    try:
        opts = {"skip_download": True, "quiet": True, "no_warnings": True,
                "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source, download=False) or {}
        for key in ("subtitles", "automatic_captions"):
            tracks = info.get(key) or {}
            for lang in list(langs) + list(tracks.keys()):
                entries = tracks.get(lang) or []
                for e in entries:
                    url = e.get("url") or ""
                    if not url:
                        continue
                    try:
                        import httpx as _httpx
                        r = _httpx.Client(timeout=30, follow_redirects=True).get(url)
                        if r.status_code != 200 or not r.text:
                            continue
                        text = _clean_subs(r.text)
                        if len(text.strip()) >= 10:
                            return text[:SUBTITLE_MAX_CHARS]
                    except Exception:
                        continue
        return ""
    except Exception as e:
        print(f"[video-subs] {source}: {e}")
        return ""


def _clean_subs(raw: str) -> str:
    """VTT/SRV3/JSON3 → text thuần, dedupe dòng, giữ thứ tự."""
    import html as _html
    import json as _json
    import re as _re

    # JSON3 (youtubeSrv3/json3): {"events": [{"segs": [{"utf8": "..."}]}]}
    raw_stripped = raw.strip()
    if raw_stripped.startswith("{"):
        try:
            data = _json.loads(raw_stripped)
            parts = []
            for ev in data.get("events", []):
                for seg in ev.get("segs", []) or []:
                    t = (seg.get("utf8") or "").strip()
                    if t and t not in parts:
                        parts.append(t)
            if parts:
                return " ".join(parts)
        except Exception:
            pass
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if _re.match(r"^\d+$", s) or "-->" in s:
            continue
        s = _re.sub(r"<[^>]+>", "", s)  # cue tags <c>, <i>...
        s = _html.unescape(s).strip()
        if s and (not lines or lines[-1] != s):
            lines.append(s)
    return " ".join(lines)

MIN_TS_GAP = 0.5  # seconds — dedup/top-up frames must differ by at least this

# ponytail: yt-dlp lazily imported; only embed hosts resolved — direct .mp4 skips it
_EMBED_RE = re.compile(r"(youtube\.com|youtu\.be|vimeo\.com|facebook\.com)", re.I)


def _resolve(source: str, tmp: str) -> str:
    """Embed page URL (youtube/vimeo/facebook) → downloaded media file in tmp.
    Other URLs returned as-is (direct .mp4 etc.)."""
    if not _EMBED_RE.search(source):
        return source
    import yt_dlp
    out = os.path.join(tmp, "resolved.%(ext)s")
    # ponytail: video-only 360p — keyframes don't need audio or 1080p
    opts = {"format": "bestvideo[height<=360]/best", "outtmpl": out, "quiet": True,
            "no_warnings": True, "noplaylist": True,
            "max_filesize": 500 * 1024 * 1024}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([source])
    files = [f for f in os.listdir(tmp) if f.startswith("resolved.")]
    if not files:
        raise RuntimeError(f"yt-dlp produced no file for {source}")
    return os.path.join(tmp, files[0])


def _download(url: str, dest: str) -> str:
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
    return dest


def _duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _pts_times(stderr: str) -> list[float]:
    """pts_time values from ffmpeg showinfo lines, in output order."""
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", stderr)]


def _run_ffmpeg(path: str, vf: str, out_pattern: str) -> list[tuple[str, float]]:
    """Run ffmpeg with showinfo appended to the filter; return [(file, pts_time)]."""
    out_dir = os.path.dirname(out_pattern)
    prefix = os.path.basename(out_pattern).split("_")[0]
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-vf", vf + ",showinfo",
         "-vsync", "vfr", out_pattern],
        capture_output=True, text=True)
    # ffmpeg may exit non-zero on truncated/stream videos even when frames were written
    times = _pts_times(proc.stderr)
    frames = sorted(f for f in os.listdir(out_dir) if f.startswith(prefix))
    if not frames:
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[-400:]}")
        return []
    return list(zip([os.path.join(out_dir, f) for f in frames], times))


def _far_from(ts: float, kept_ts: list[float], gap: float = MIN_TS_GAP) -> bool:
    return all(abs(ts - t) >= gap for t in kept_ts)


KEYFRAME_INTERVAL = 10.0  # seconds — first frame at 0s, then one every 10s (giảm tải VLM: nửa số frame/video)


def extract_keyframes(source: str, max_frames: int | None = None,
                      interval: float = KEYFRAME_INTERVAL) -> list[tuple[bytes, float]]:
    """source = local path or http(s) URL. Returns [(png_bytes, pts_time_seconds)].

    Fixed interval: frame at 0s, then one every `interval` seconds, capped at
    max_frames.
    """
    cfg = get_settings().video
    max_frames = max_frames or cfg.max_keyframes

    tmp = tempfile.mkdtemp(prefix="sict_video_")
    try:
        path = source
        if source.startswith(("http://", "https://")):
            resolved = _resolve(source, tmp)  # embed URL → local file, else same URL
            if not os.path.isfile(resolved):
                resolved = _download(resolved, os.path.join(tmp, "input.mp4"))
            path = resolved
        else:
            path = source

        picked: list[tuple[str, float]] = _run_ffmpeg(
            # ponytail: isnan(prev_selected_t) picks t=0, then gte(t, prev+interval)
            path, f"select='isnan(prev_selected_t)+gte(t\\,prev_selected_t+{interval})',"
                  f"scale=640:-1",
            os.path.join(tmp, "fixed_%03d.png"))

        picked = picked[:max_frames]
        return [(open(f, "rb").read(), t) for f, t in picked]
    except Exception as e:
        print(f"[video] {source}: {e}")
        return []  # caller skips empty
    finally:
        subprocess.run(["rm", "-rf", tmp])


def dedup_indices(vecs: list[list[float]], threshold: float | None = None) -> list[int]:
    """Greedy dedup: keep first; drop any vector with cosine > threshold to a kept one.

    Vectors are L2-normalized (embedder) → cosine == dot. Pure python, ≤20×512.
    """
    cfg = get_settings().video
    threshold = threshold if threshold is not None else cfg.dedup_threshold
    kept: list[list[float]] = []
    keep_idx: list[int] = []
    for i, v in enumerate(vecs):
        if all(sum(a * b for a, b in zip(v, k)) <= threshold for k in kept):
            kept.append(v)
            keep_idx.append(i)
    return keep_idx

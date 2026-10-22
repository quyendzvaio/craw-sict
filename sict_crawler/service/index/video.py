"""Video → keyframes via ffmpeg + subtitle-only transcript (no ASR).

Subtitle: chỉ lấy caption/subtitle có sẵn của YouTube (hoặc host yt-dlp
hỗ trợ). Không có → trả "" và caller bỏ qua luôn, KHÔNG chạy Whisper.

Cookie: file Netscape cookies.txt ở $YT_COOKIES (mặc định /cookies/cookies.txt,
mount từ ./cookies/ trên worker). Hết hạn → xoay qua workflow cookies.yml
(secret COOKIES_TXT), không commit cookie vào git.

KHÔNG BAO GIỜ lưu full video: keyframe đọc stream trực tiếp qua ffmpeg
(download fallback chỉ vào tmp và xóa ngay trong finally).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

import httpx

from sict_crawler.core.config import get_settings

MIN_TS_GAP = 0.5  # seconds — dedup/top-up frames must differ by at least this

# ponytail: yt-dlp lazily imported; only embed hosts resolved — direct .mp4 skips it
_EMBED_RE = re.compile(r"(youtube\.com|youtu\.be|vimeo\.com|facebook\.com)", re.I)

SUBTITLE_LANGS = ("vi", "en")
SUBTITLE_MAX_CHARS = 2000

# 360p đủ cho OCR lower-third + CLIP, nhẹ băng thông/disk
_STREAM_FORMAT = "bestvideo[height<=360]/best[height<=360]/best"


def _cookie_opts() -> dict:
    """Trả {"cookiefile": path} nếu file cookie tồn tại, else {}.

    yt-dlp ghi session cookies TRỞ LẠI file khi đóng (save_cookies), nên
    file phải ghi được bởi user chạy worker (uid 10000 trong container).
    File mount từ host thường chỉ đọc được → copy sang tmp của process.
    """
    import shutil
    import tempfile

    src = os.environ.get("YT_COOKIES", "/cookies/cookies.txt")
    if not src or not os.path.isfile(src):
        return {}
    if os.access(src, os.W_OK):
        return {"cookiefile": src}
    dst = os.path.join(tempfile.gettempdir(),
                       "sict_cookies_%d.txt" % os.geteuid())
    try:
        if (not os.path.isfile(dst)
                or os.path.getmtime(src) > os.path.getmtime(dst)):
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
    except Exception as e:
        print(f"[video-cookie] copy failed: {e}")
        return {}
    return {"cookiefile": dst}


def _extract_info(source: str, storyboard: bool = False) -> dict:
    """Một lần extract_info cho mọi nhu cầu (stream/thông tin/sub).

    skip_download=True → KHÔNG tải full video về disk.
    YouTube 2026 chặn client web từ IP lạ: thử client mặc định trước,
    rớt thì thử ios. storyboard=True chọn format sprite sb0/sb1/sb2
    (luôn tồn tại) để vẫn lấy được metadata/sub/storyboard.
    """
    import yt_dlp

    def _run(client):
        opts = {"skip_download": True, "quiet": True, "no_warnings": True,
                "noplaylist": True,
                "format": "sb0/sb1/sb2" if storyboard else _STREAM_FORMAT,
                **_cookie_opts()}
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(source, download=False) or {}

    last = None
    for client in (None, "ios"):
        try:
            return _run(client)
        except Exception as e:
            last = e
    print(f"[video-info] {source}: {last}")
    return {}


def _stream_url(info: dict) -> str:
    """URL media trực tiếp cho ffmpeg (không tải file).

    Bỏ qua storyboard/sprite (mhtml) — chúng đi đường riêng.
    """
    if info.get("url") and info.get("ext") != "mhtml":
        return info["url"]
    for f in info.get("requested_formats") or []:
        if _playable(f):
            return f["url"]
    for f in info.get("formats") or []:
        if _playable(f) and (f.get("height") or 9999) <= 360:
            return f["url"]
    return ""


def _playable(f: dict) -> bool:
    return bool(f.get("url") and f.get("vcodec") not in (None, "none")
                and f.get("ext") != "mhtml"
                and not str(f.get("format_id", "")).startswith("sb"))


def _storyboard_frames(source: str, tmp: str,
                       max_frames: int) -> list[tuple[bytes, float]]:
    """Keyframe từ sprite storyboard (không cần stream play được).

    Mỗi spec: rows×cols tile/sprite, mỗi fragment có duration. Cắt tile
    bằng PIL, timestamp = vị trí tile trong video. Không tải full video.
    """
    from PIL import Image

    info = _extract_info(source, storyboard=True)
    specs = [f for f in info.get("formats", [])
             if str(f.get("format_id", "")).startswith("sb")]
    # sprite to (sb0 160px) trước để OCR đọc được
    specs.sort(key=lambda f: -(int(f.get("width") or 0)))
    tiles: list[tuple[bytes, float]] = []
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=60, follow_redirects=True) as c:
            for spec in specs:
                rows = int(spec.get("rows") or 0)
                cols = int(spec.get("columns") or 0)
                if rows <= 0 or cols <= 0:
                    continue
                offset = 0.0
                for frag in spec.get("fragments") or []:
                    furl = frag.get("url") or ""
                    dur = float(frag.get("duration") or 0)
                    if not furl or dur <= 0:
                        continue
                    r = c.get(furl)
                    if r.status_code != 200 or len(r.content) < 1000:
                        continue
                    try:
                        img = Image.open(__import__("io").BytesIO(r.content))
                    except Exception:
                        continue
                    w, h = img.size
                    tw, th = w // cols, h // rows
                    if tw <= 0 or th <= 0:
                        continue
                    per = dur / (rows * cols)
                    for i in range(rows * cols):
                        x, y = (i % cols) * tw, (i // cols) * th
                        tile = img.crop((x, y, x + tw, y + th))
                        import io as _io
                        buf = _io.BytesIO()
                        tile.save(buf, format="PNG")
                        tiles.append((buf.getvalue(), offset + i * per))
                    offset += dur
                if tiles:
                    break  # spec to nhất đủ dùng
    except Exception as e:
        print(f"[video-sb] {source}: {e}")
        return []
    if len(tiles) <= max_frames:
        return tiles
    step = len(tiles) / max_frames  # lấy đều, giữ mốc 0s
    return [tiles[int(i * step)] for i in range(max_frames)]


def fetch_video_info(source: str,
                     langs: tuple[str, ...] = SUBTITLE_LANGS) -> dict:
    """Thông tin video + subtitle trong 1 lần extract (không tải video).

    Trả {"title", "duration", "uploader", "subs"}; lỗi/block → dict rỗng
    các trường ""/0 (caller bỏ qua êm).
    """
    out = {"title": "", "duration": 0, "uploader": "", "subs": ""}
    if not _EMBED_RE.search(source):
        return out
    try:
        import yt_dlp  # noqa: F401  (giữ import rõ để báo thiếu deps)
    except ImportError:
        return out
    info = _extract_info(source)
    if not info.get("title"):
        # stream play bị chặn nhưng metadata/sub vẫn lấy được qua sprite
        info = _extract_info(source, storyboard=True)
    if not info:
        return out
    out["title"] = (info.get("title") or "")[:300]
    out["duration"] = info.get("duration") or 0
    out["uploader"] = (info.get("uploader") or "")[:200]
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        for lang in list(langs) + list(tracks.keys()):
            for e in tracks.get(lang) or []:
                url = e.get("url") or ""
                if not url:
                    continue
                try:
                    r = httpx.Client(timeout=30,
                                     follow_redirects=True).get(url)
                    if r.status_code != 200 or not r.text:
                        continue
                    text = _clean_subs(r.text)
                    if len(text.strip()) >= 10:
                        out["subs"] = text[:SUBTITLE_MAX_CHARS]
                        return out
                except Exception:
                    continue
    return out


def fetch_subtitles(source: str, langs: tuple[str, ...] = SUBTITLE_LANGS) -> str:
    """Lấy subtitle có sẵn (manual trước, auto sau). Không có → "".

    Chỉ resolve embed URL (youtube/vimeo/facebook). File .mp4 trực tiếp
    không có subtitle → "" luôn. Không tải video, chỉ fetch file vtt/json.
    """
    return fetch_video_info(source, langs).get("subs", "")


def fetch_video_title(source: str) -> str:
    """Tiêu đề video để merge vào caption (không tải video)."""
    if not _EMBED_RE.search(source):
        return ""
    try:
        return fetch_video_info(source).get("title", "")
    except Exception:
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

def _resolve(source: str, tmp: str) -> str:
    """Fallback: tải file về tmp (chỉ khi stream trực tiếp thất bại).

    File trong tmp LUÔN bị xóa ở finally của extract_keyframes —
    không bao giờ lưu full video lâu dài.
    """
    if not _EMBED_RE.search(source):
        return source
    import yt_dlp
    out = os.path.join(tmp, "resolved.%(ext)s")
    # ponytail: video-only 360p — keyframes don't need audio or 1080p
    opts = {"format": "bestvideo[height<=360]/best", "outtmpl": out, "quiet": True,
            "no_warnings": True, "noplaylist": True,
            "max_filesize": 500 * 1024 * 1024, **_cookie_opts()}
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
            if _EMBED_RE.search(source):
                # Ưu tiên 1: đọc stream trực tiếp (KHÔNG tải full video).
                # Ưu tiên 2: sprite storyboard (không cần stream play được).
                # Fallback: tải về tmp rồi xóa ngay (giữ hành vi cũ).
                stream = ""
                try:
                    stream = _stream_url(_extract_info(source))
                except Exception as e:
                    print(f"[video-stream] {source}: {e}")
                if stream:
                    path = stream
                else:
                    sb = _storyboard_frames(
                        source, tmp,
                        max_frames or get_settings().video.max_keyframes)
                    if sb:
                        return sb
                    resolved = _resolve(source, tmp)
                    if not os.path.isfile(resolved):
                        resolved = _download(resolved,
                                             os.path.join(tmp, "input.mp4"))
                    path = resolved
            else:
                path = _download(source, os.path.join(tmp, "input.mp4"))

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

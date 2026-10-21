"""S3 storage for raw HTML + media. Image → base64 in DB; video keyframes → S3.

Local-disk fallback when AWS credentials missing (dev mode).
Callers: crawler.py (put_raw_html), parser.py (put_media), main.py.
Schemas: S3 keys — {prefix_raw}{yyyy}/{mm}/{sha256}.html.gz, {prefix_media}{sha256}.{ext}
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3

from .config import get_settings

LOCAL_ROOT = Path(os.environ.get("SICT_LOCAL_STORE", "./_local_store"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def from_base64(s: str) -> bytes:
    return base64.b64decode(s)


class Storage:
    def __init__(self):
        cfg = get_settings()
        self.cfg = cfg.s3
        self.local = not ((cfg.s3.access_key and cfg.s3.secret_key)
                          or cfg.s3.profile)
        if self.local:
            LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
            return
        kwargs: dict = dict(region_name=cfg.s3.region)
        if cfg.s3.access_key and cfg.s3.secret_key:
            kwargs.update(aws_access_key_id=cfg.s3.access_key,
                          aws_secret_access_key=cfg.s3.secret_key)
        # MinIO / LocalStack: S3_ENDPOINT_URL=http://minio:9000
        if cfg.s3.endpoint_url:
            kwargs["endpoint_url"] = cfg.s3.endpoint_url
        if cfg.s3.profile and not (cfg.s3.access_key and cfg.s3.secret_key):
            import boto3 as _boto3
            self.s3 = _boto3.Session(
                profile_name=cfg.s3.profile).client("s3", **kwargs)
        else:
            self.s3 = boto3.client("s3", **kwargs)

    def _put(self, key: str, body: bytes) -> None:
        if self.local:
            p = LOCAL_ROOT / key
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
        else:
            self.s3.put_object(Bucket=self.cfg.bucket, Key=key, Body=body)

    def put_raw_html(self, url: str, html: bytes) -> str:
        now = datetime.now(timezone.utc)
        key = f"{self.cfg.prefix_raw}{now:%Y}/{now:%m}/{sha256(html)}.html.gz"
        self._put(key, gzip.compress(html))
        return key

    def _get(self, key: str) -> bytes:
        """S3 get, NoSuchKey -> FileNotFoundError de caller fallback re-fetch."""
        try:
            return self.s3.get_object(Bucket=self.cfg.bucket,
                                      Key=key)["Body"].read()
        except Exception as e:
            resp = getattr(e, "response", None) or {}
            code = (resp.get("Error") or {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket") \
                    or "NoSuchKey" in str(e):
                raise FileNotFoundError(key) from e
            raise

    def read_raw_html(self, key: str) -> bytes:
        if self.local:
            return gzip.decompress((LOCAL_ROOT / key).read_bytes())
        return gzip.decompress(self._get(key))

    def put_media(self, data: bytes, ext: str) -> tuple[str, str]:
        """Store media bytes → (key, base64)."""
        key = f"{self.cfg.prefix_media}{sha256(data)}.{ext}"
        self._put(key, data)
        return key, to_base64(data)

    def exists(self, key: str) -> bool:
        if self.local:
            return (LOCAL_ROOT / key).exists()
        try:
            self.s3.head_object(Bucket=self.cfg.bucket, Key=key)
            return True
        except Exception:
            return False

    def put_file(self, key: str, path: Path) -> bool:
        """Đưa file có sẵn (snapshot/artifact) lên store nếu chưa có. Local: hard link khi cùng
        ổ (không tốn thêm dung lượng), không được thì copy. Trả True khi có ghi mới."""
        if self.exists(key):
            return False
        if self.local:
            dst = LOCAL_ROOT / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, dst)
            except OSError:
                dst.write_bytes(Path(path).read_bytes())
            return True
        self.s3.upload_file(str(path), self.cfg.bucket, key)
        return True

    def get_media(self, key: str) -> bytes:
        if self.local:
            return (LOCAL_ROOT / key).read_bytes()
        return self.s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read()

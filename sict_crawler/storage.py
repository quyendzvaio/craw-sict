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

    def read_raw_html(self, key: str) -> bytes:
        if self.local:
            return gzip.decompress((LOCAL_ROOT / key).read_bytes())
        body = self.s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read()
        return gzip.decompress(body)

    def put_media(self, data: bytes, ext: str) -> tuple[str, str]:
        """Store media bytes → (key, base64)."""
        key = f"{self.cfg.prefix_media}{sha256(data)}.{ext}"
        self._put(key, data)
        return key, to_base64(data)

    def get_media(self, key: str) -> bytes:
        if self.local:
            return (LOCAL_ROOT / key).read_bytes()
        return self.s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read()

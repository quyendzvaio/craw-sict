"""Verify S3 migration: compare DB keys vs S3 keys, optionally heal orphans.

Orphan = DB row whose s3_key is missing on S3 (file lost locally too).
Media orphans WITH base64_data can be healed by re-upload (keys are
content-hash deterministic → bytes identical).

Usage:
  .venv/bin/python scripts/verify_s3.py            # report only
  .venv/bin/python scripts/verify_s3.py --heal     # re-upload healable orphans
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

os.environ.setdefault("AWS_PROFILE", "quyen-admin")

from sqlalchemy import select  # noqa: E402

from sict_crawler.model.orm import Document, MediaAsset  # noqa: E402
from sict_crawler.repository import db  # noqa: E402
from sict_crawler.repository import storage as S


def list_s3_keys(st, prefix: str) -> set[str]:
    keys: set[str] = set()
    token = None
    while True:
        kw = {"Bucket": st.cfg.bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = st.s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            keys.add(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp["NextContinuationToken"]
    return keys


async def main(heal: bool = False) -> None:
    st = S.Storage()
    assert not st.local, "Storage is local — set AWS_PROFILE/keys first"
    print("listing S3 keys...", flush=True)
    s3_media = list_s3_keys(st, "media/")
    s3_raw = list_s3_keys(st, "raw/")
    print(f"S3: {len(s3_media)} media, {len(s3_raw)} raw", flush=True)

    orphans_healable = orphans_dead = raw_missing = checked = 0
    healed = 0
    async with db.get_session() as s:
        ids = (await s.scalars(select(MediaAsset.id).order_by(MediaAsset.id))).all()
        for i in range(0, len(ids), 500):
            chunk = (await s.scalars(
                select(MediaAsset).where(MediaAsset.id.in_(ids[i:i + 500])))).all()
            for a in chunk:
                checked += 1
                if not a.s3_key or a.s3_key in s3_media:
                    continue
                if a.base64_data:
                    orphans_healable += 1
                    if heal:
                        st.s3.put_object(Bucket=st.cfg.bucket, Key=a.s3_key,
                                         Body=S.from_base64(a.base64_data))
                        healed += 1
                else:
                    orphans_dead += 1
            if checked % 5000 == 0:
                print(f"  ...{checked}/{len(ids)} media checked", flush=True)
        raw_keys = (await s.scalars(
            select(Document.raw_s3_key).where(Document.raw_s3_key != ""))).all()
        for k in raw_keys:
            if k not in s3_raw:
                raw_missing += 1
    print(f"MEDIA checked={checked} healable={orphans_healable} "
          f"dead={orphans_dead} healed={healed} raw_missing={raw_missing}")


if __name__ == "__main__":
    asyncio.run(main(heal="--heal" in sys.argv))

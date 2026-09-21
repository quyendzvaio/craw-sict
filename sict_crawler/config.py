"""Load config.yaml into typed settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = os.environ.get("SICT_CONFIG", str(Path(__file__).parent.parent / "config.yaml"))


@dataclass
class SiteConfig:
    base_url: str
    seed_urls: list[str]
    skip_patterns: list[str]
    external_hosts: list[str] = field(default_factory=list)


@dataclass
class CrawlConfig:
    max_depth: int = 5
    max_pages: int = 1000
    delay_seconds: float = 1.0
    timeout_seconds: int = 30
    user_agent: str = "SICT-RAG-Crawler/1.0"
    respect_robots: bool = True


@dataclass
class QdrantConfig:
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    prefer_grpc: bool = True
    timeout: int = 30  # giây; worker batch ghi đặt cao hơn qua QDRANT_TIMEOUT
    collection_text: str = "sict_text"
    collection_image: str = "sict_image"
    text_dim: int = 384
    image_dim: int = 512


@dataclass
class S3Config:
    bucket: str
    region: str = "ap-northeast-1"
    prefix_raw: str = "raw/"
    prefix_media: str = "media/"
    access_key: str = ""
    secret_key: str = ""
    profile: str = ""  # AWS_PROFILE: dùng ~/.aws/credentials thay vì key rời
    endpoint_url: str = ""  # MinIO: http://minio:9000 (S3 tương thích)

    def __post_init__(self):
        self.access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.profile = os.environ.get("AWS_PROFILE", "")
        # S3_ENDPOINT_URL set → dùng MinIO/LocalStack thay vì AWS
        self.endpoint_url = os.environ.get("S3_ENDPOINT_URL", "")


@dataclass
class EmbeddingConfig:
    text_model: str = "intfloat/multilingual-e5-small"
    image_model: str = "openai/clip-vit-base-patch32"
    device: str = "cpu"
    batch_size: int = 32


@dataclass
class ChunkConfig:
    min_tokens: int = 200
    max_tokens: int = 500
    overlap_ratio: float = 0.1
    semantic: bool = True


@dataclass
class VideoConfig:
    scene_threshold: float = 0.3
    max_keyframes: int = 20
    min_keyframes: int = 3
    dedup_threshold: float = 0.8  # drop keyframes with cosine similarity > this


@dataclass
class VlmConfig:
    api_base: str = "http://localhost:20128/v1"
    api_key: str = ""  # set via VLM_API_KEY env var
    models: list[str] = field(default_factory=lambda: ["VLM"])
    max_new_tokens: int = 128
    caption_all: bool = False  # False → caption only assets with faces
    per_page_limit: int = 10
    enabled: bool = True  # False → pipeline annotate bỏ hết VLM (dùng OCR local)

    def __post_init__(self):
        self.api_key = os.environ.get("VLM_API_KEY", self.api_key)
        # Override provider qua env (không cần sửa yaml/rebuild):
        # VLM_API_BASE (VD https://api.openai.com/v1), VLM_MODELS (phân tách dấu phẩy),
        # VLM_ENABLED (true/false).
        if os.environ.get("VLM_API_BASE"):
            self.api_base = os.environ["VLM_API_BASE"]
        if os.environ.get("VLM_MODELS"):
            self.models = [m.strip() for m in os.environ["VLM_MODELS"].split(",") if m.strip()]
        if os.environ.get("VLM_ENABLED"):
            self.enabled = os.environ["VLM_ENABLED"].lower() in ("1", "true", "yes", "on")


@dataclass
class LlmConfig:
    """Text LLM for query rewriting (and later answers)."""
    api_base: str = "http://localhost:20128/v1"
    api_key: str = ""  # set via LLM_API_KEY env var
    model: str = "sict"
    max_new_tokens: int = 256

    def __post_init__(self):
        self.api_key = os.environ.get("LLM_API_KEY", self.api_key)
        # Override provider qua env: LLM_API_BASE, LLM_MODEL.
        if os.environ.get("LLM_API_BASE"):
            self.api_base = os.environ["LLM_API_BASE"]
        if os.environ.get("LLM_MODEL"):
            self.model = os.environ["LLM_MODEL"]


@dataclass
class Settings:
    site: SiteConfig
    crawl: CrawlConfig
    postgres_dsn: str
    qdrant: QdrantConfig
    redis_url: str
    celery_broker_url: str
    celery_result_url: str
    s3: S3Config
    embedding: EmbeddingConfig
    chunking: ChunkConfig
    video: VideoConfig
    vlm: VlmConfig
    llm: LlmConfig


def _build(data: dict[str, Any]) -> Settings:
    site = SiteConfig(**data["site"])
    crawl = CrawlConfig(**data.get("crawl", {}))
    # Env override (12-factor cho k8s): không set → dùng yaml như cũ.
    qdata = dict(data.get("qdrant", {}))
    if os.environ.get("QDRANT_HOST"):
        qdata["host"] = os.environ["QDRANT_HOST"]
    if os.environ.get("QDRANT_PORT"):
        qdata["port"] = int(os.environ["QDRANT_PORT"])
    if os.environ.get("QDRANT_GRPC_PORT"):
        qdata["grpc_port"] = int(os.environ["QDRANT_GRPC_PORT"])
    if os.environ.get("QDRANT_TIMEOUT"):
        qdata["timeout"] = int(os.environ["QDRANT_TIMEOUT"])
    qdrant = QdrantConfig(**qdata)
    s3 = S3Config(**data["s3"])
    emb = EmbeddingConfig(**data.get("embedding", {}))
    chunk = ChunkConfig(**data.get("chunking", {}))
    video = VideoConfig(**data.get("video", {}))
    vlm = VlmConfig(**data.get("vlm", {}))
    llm = LlmConfig(**data.get("llm", {}))
    redis_url = os.environ.get("REDIS_URL", data["redis"]["url"])
    return Settings(
        site=site,
        crawl=crawl,
        postgres_dsn=os.environ.get("POSTGRES_DSN", data["postgres"]["dsn"]),
        qdrant=qdrant,
        redis_url=redis_url,
        celery_broker_url=os.environ.get("CELERY_BROKER_URL", data["celery"]["broker_url"]),
        celery_result_url=os.environ.get("CELERY_RESULT_URL", data["celery"]["result_url"]),
        s3=s3,
        embedding=emb,
        chunking=chunk,
        video=video,
        vlm=vlm,
        llm=llm,
    )


_settings: Settings | None = None


def get_settings(path: str | None = None) -> Settings:
    global _settings
    if _settings is None or path is not None:
        with open(path or CONFIG_PATH) as f:
            _settings = _build(yaml.safe_load(f))
    return _settings

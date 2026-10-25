"""Load config.yaml into typed settings."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = os.environ.get("SICT_CONFIG", str(Path(__file__).resolve().parents[2] / "config.yaml"))


def _env_bool(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "on")


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
    # incremental: mỗi lượt tải lại trang 1 của MỌI mục danh sách, đi tiếp trang 2, 3… khi trang còn bài lạ
    listing_max_pages: int = 5
    # tải lỗi tạm thời (timeout, 5xx, thân rỗng): thử lại ở các lượt sau, tối đa N lần rồi bỏ
    max_fetch_retries: int = 3


@dataclass
class QdrantConfig:
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    prefer_grpc: bool = True
    timeout: int = 30  # giây; worker batch ghi đặt cao hơn qua QDRANT_TIMEOUT
    collection_text: str = "sict_text_v2"
    collection_image: str = "sict_image_v2"
    # caption ảnh/video (tiêu đề + mục + chữ OCR + phụ đề), cùng không gian vector với chữ. Rỗng = chung
    # collection_text như v1/v2; index v3 tách riêng (sict_ocr_v3) — vector_store.ocr_collection()
    collection_ocr: str = ""
    text_dim: int = 384
    image_dim: int = 512


@dataclass
class S3Config:
    bucket: str
    region: str = "us-east-2"   # cùng region VM1/VM2 (D14); config.yaml ghi đè
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
    device: str = "auto"  # auto | cpu | cuda[:N] — auto: có GPU thì dùng, không thì CPU
    batch_size: int = 32
    # D22: backend=api → một model đa phương thức (chữ+ảnh chung không gian) qua endpoint HTTP.
    # backend=torch giữ E5 + CLIP cho index v2 tới khi cutover.
    backend: str = "torch"          # torch | api
    api_base: str = ""              # EMBED_API_BASE (Jina cloud: https://api.jina.ai/v1; tự host: URL service)
    api_key: str = ""               # EMBED_API_KEY, không ghi vào yaml
    model: str = "jina-clip-v2"     # EMBED_MODEL
    dim: int = 1024                 # EMBED_DIM; Matryoshka: có thể cắt xuống (64–1024)
    query_prefix: str = "Represent the query for retrieving evidence documents: "
    api_batch: int = 32             # số chữ mỗi lượt gọi
    api_image_batch: int = 8        # số ảnh mỗi lượt gọi (ảnh nặng token)
    api_retries: int = 8
    api_workers: int = 1            # số lượt gọi song song (EMBED_WORKERS); cloud trả phí nên đặt 8–16
    api_tpm: int = 0                # giới hạn token/phút phía client (EMBED_TPM); 0 = không giới hạn
    timeout: int = 120
    cache_path: str = ""            # sqlite cache vector theo hash đầu vào; rỗng = không cache
    # cạnh ảnh TRUY VẤN gửi dịch vụ tự host ("image_size" trong mục ảnh; EMBED_QUERY_IMAGE_SIZE); 0 = như ảnh
    # corpus (512). Chỉ đặt khi EMBED_API_BASE là embed_server (Jina cloud không nhận khoá này). Production: 336 (2026-10-25; trước đó 392).
    query_image_size: int = 0

    def __post_init__(self):
        # EMBED_DEVICE override (VD ép cpu trên máy có GPU dùng chung)
        if os.environ.get("EMBED_DEVICE"):
            self.device = os.environ["EMBED_DEVICE"].strip().lower()
        # tên biến chủ dự án đặt trong .env (BASEURL_EMBEDDING / APIKEY_EMBEDDING_MODEL / EMBEDDING_MODEL) dùng
        # làm dự phòng khi không có EMBED_*
        alt = {"api_base": "BASEURL_EMBEDDING", "api_key": "APIKEY_EMBEDDING_MODEL", "model": "EMBEDDING_MODEL"}
        for env, attr in (("EMBED_BACKEND", "backend"), ("EMBED_API_BASE", "api_base"),
                          ("EMBED_API_KEY", "api_key"), ("EMBED_MODEL", "model"),
                          ("EMBED_DIM", "dim"), ("EMBED_CACHE_PATH", "cache_path"), ("EMBED_TPM", "api_tpm"),
                          ("EMBED_WORKERS", "api_workers"), ("EMBED_IMAGE_BATCH", "api_image_batch"),
                          ("EMBED_QUERY_IMAGE_SIZE", "query_image_size")):
            v = os.environ.get(env) or os.environ.get(alt.get(attr, ""), "")
            if v.strip():   # kiểu lấy theo giá trị mặc định (dim → int)
                setattr(self, attr, type(getattr(self, attr))(v.strip()))
        # BASEURL có thể là URL đầy đủ ".../v1/embeddings"; embedder tự thêm "/embeddings"
        self.api_base = re.sub(r"/embeddings/?$", "", self.api_base.strip())
        # EMBEDDING_MODEL có thể là link trang model ("https://jina.ai?sui=embeddings&model=jina-clip-v2")
        if "://" in self.model:
            m = re.search(r"[?&]model=([^&#]+)", self.model)
            self.model = m.group(1) if m else "jina-clip-v2"
        self.backend = self.backend.lower()
        if self.backend not in ("torch", "api"):
            raise ValueError(f"embedding.backend không hợp lệ: {self.backend!r} (torch | api)")


@dataclass
class ChunkConfig:
    min_tokens: int = 200
    max_tokens: int = 500
    overlap_ratio: float = 0.1
    semantic: bool = True
    structured: bool = False   # D23: chunk theo block (heading_path, bảng giữ mục); index v3 bật (CHUNK_STRUCTURED=1)

    def __post_init__(self):
        if os.environ.get("CHUNK_STRUCTURED"):
            self.structured = _env_bool(os.environ["CHUNK_STRUCTURED"])


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
            self.enabled = _env_bool(os.environ["VLM_ENABLED"])


@dataclass
class LlmConfig:
    """Text LLM for query rewriting (and later answers)."""
    api_base: str = "http://localhost:20128/v1"
    api_key: str = ""  # set via LLM_API_KEY env var
    model: str = "sict"
    max_new_tokens: int = 256
    timeout: int = 60       # giây / lượt gọi; model suy luận (VD deepseek qua gateway) cần lâu hơn
    # D21: gửi cả ảnh người dùng vào lượt trả lời (cần model hỗ trợ ảnh). Tắt = chỉ retrieval, LLM thấy chữ tìm được.
    answer_with_image: bool = False
    # Lượt LLM viết lại câu hỏi + đoán media (vlm.rewrite_and_route). Tắt mặc định (2026-10-25): tốn 2–11 s / câu mà không
    # đo được lợi ích (golden recall@5 0,985 cả hai; MRR 0,898 không viết lại vs 0,888; media_mode 0,871 vs 0,886)
    # → chỉ còn luật viết tắt (query_rewriter._rule_rewrite) + luật router. Bật lại: LLM_REWRITE_QUERY=1.
    rewrite_query: bool = False
    # D27: ngân sách ngữ cảnh gửi LLM (ký tự) — mỗi nguồn tối đa source_chars, tổng ≈ context_chars (chia đều khi nhiều
    # nguồn). Trước đây LLM chỉ thấy 300 ký tự đầu mỗi đoạn (excerpt hiển thị trên UI).
    source_chars: int = 1500
    context_chars: int = 9000

    def __post_init__(self):
        self.api_key = os.environ.get("LLM_API_KEY", self.api_key)
        # Override provider qua env: LLM_API_BASE, LLM_MODEL, LLM_MAX_NEW_TOKENS, LLM_TIMEOUT.
        if os.environ.get("LLM_API_BASE"):
            self.api_base = os.environ["LLM_API_BASE"]
        if os.environ.get("LLM_MODEL"):
            self.model = os.environ["LLM_MODEL"]
        # model suy luận tiêu token "nghĩ" trước khi trả lời → 256 có thể ra câu trả lời rỗng
        if os.environ.get("LLM_MAX_NEW_TOKENS"):
            self.max_new_tokens = int(os.environ["LLM_MAX_NEW_TOKENS"])
        if os.environ.get("LLM_TIMEOUT"):
            self.timeout = int(os.environ["LLM_TIMEOUT"])
        if os.environ.get("LLM_ANSWER_WITH_IMAGE"):
            self.answer_with_image = _env_bool(os.environ["LLM_ANSWER_WITH_IMAGE"])
        if os.environ.get("LLM_REWRITE_QUERY"):
            self.rewrite_query = _env_bool(os.environ["LLM_REWRITE_QUERY"])


@dataclass
class RouterConfig:
    """Ngưỡng chặn media theo bằng chứng (router.gate). TẠM — hiệu chỉnh bằng bộ câu hỏi vàng."""
    clip_min: float = 0.24       # cosine CLIP text↔ảnh (B/32: khớp ~0.25–0.33, lạc đề ~0.15–0.2)
    caption_min: float = 0.82    # cosine E5 câu hỏi↔caption (E5 nén điểm: lạc đề ~0.72–0.78)
    optional_max: int = 3        # media tối đa ở mức optional
    optional_top_docs: int = 3   # optional: chỉ media thuộc N bài chữ đứng đầu
    graph_min: float = 0.6       # media gắn thực thể trong câu hỏi (graph D20): trọng số cạnh tối thiểu
    # D21/D22 — không gian jina chung chữ+ảnh, ngưỡng tách theo cặp (truy vấn → đích). TẠM tới khi hiệu chỉnh
    # bằng eval/golden_images_v1 (scripts/eval_multimodal.py). Chỉ dùng khi embedding.backend=api.
    t2i_min: float = 0.25        # chữ → ảnh
    t2c_min: float = 0.5         # chữ → caption (cosine câu hỏi ↔ caption cùng không gian)
    i2i_min: float = 0.6         # ảnh → ảnh
    i2t_min: float = 0.3         # ảnh → chữ/caption (chỉ lọc khi truy vấn CHỈ có ảnh)


@dataclass
class RetrievalConfig:
    """Guardrail truy xuất không phụ thuộc LLM (D27). Ngưỡng TẠM — hiệu chỉnh bằng eval/golden_v1 + eval/list_v1."""
    per_doc_max: int = 2         # tối đa N đoạn của cùng 1 bài trong top k (đoạn bị đẩy xếp sau, không mất)
    list_top_k: int = 10         # câu hỏi liệt kê ("có những … nào"): số nguồn tối thiểu, mỗi bài 1 đoạn
    min_cosine: float = 0.45     # sufficient: cosine câu hỏi↔đoạn tốt nhất tối thiểu (jina-clip-v2, index v3)
    min_term_cover: float = 0.8  # sufficient: tỷ lệ từ nội dung của câu hỏi (bỏ dấu, bỏ hư từ) có trong ngữ cảnh


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
    router: RouterConfig = field(default_factory=RouterConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)


def _build(data: dict[str, Any]) -> Settings:
    site = SiteConfig(**data["site"])
    crawl = CrawlConfig(**data.get("crawl", {}))
    # Env override (12-factor cho compose/.env): không set → dùng yaml như cũ.
    qdata = dict(data.get("qdrant", {}))
    if os.environ.get("QDRANT_HOST"):
        qdata["host"] = os.environ["QDRANT_HOST"]
    if os.environ.get("QDRANT_PORT"):
        qdata["port"] = int(os.environ["QDRANT_PORT"])
    if os.environ.get("QDRANT_GRPC_PORT"):
        qdata["grpc_port"] = int(os.environ["QDRANT_GRPC_PORT"])
    if os.environ.get("QDRANT_TIMEOUT"):
        qdata["timeout"] = int(os.environ["QDRANT_TIMEOUT"])
    # cutover blue/green (D8): trỏ API/worker sang collection *_v2 mà không sửa yaml
    if os.environ.get("QDRANT_COLLECTION_TEXT"):
        qdata["collection_text"] = os.environ["QDRANT_COLLECTION_TEXT"]
    if os.environ.get("QDRANT_COLLECTION_IMAGE"):
        qdata["collection_image"] = os.environ["QDRANT_COLLECTION_IMAGE"]
    if os.environ.get("QDRANT_COLLECTION_OCR"):
        qdata["collection_ocr"] = os.environ["QDRANT_COLLECTION_OCR"]
    # index v3 (jina-clip-v2): chữ và ảnh cùng số chiều (EMBED_DIM), bật khi backend=api
    for env, key in (("QDRANT_TEXT_DIM", "text_dim"), ("QDRANT_IMAGE_DIM", "image_dim")):
        if os.environ.get(env):
            qdata[key] = int(os.environ[env])
    qdrant = QdrantConfig(**qdata)
    s3 = S3Config(**data["s3"])
    emb = EmbeddingConfig(**data.get("embedding", {}))
    chunk = ChunkConfig(**data.get("chunking", {}))
    video = VideoConfig(**data.get("video", {}))
    vlm = VlmConfig(**data.get("vlm", {}))
    llm = LlmConfig(**data.get("llm", {}))
    router = RouterConfig(**(data.get("router") or {}))
    retrieval = RetrievalConfig(**(data.get("retrieval") or {}))
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
        router=router,
        retrieval=retrieval,
    )


_settings: Settings | None = None


def get_settings(path: str | None = None) -> Settings:
    global _settings
    if _settings is None or path is not None:
        with open(path or CONFIG_PATH) as f:
            _settings = _build(yaml.safe_load(f))
    return _settings

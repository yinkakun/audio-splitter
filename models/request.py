from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StorageConfig:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str


@dataclass(frozen=True)
class RedisConfig:
    url: str


@dataclass(frozen=True)
class AudioCacheConfig:
    redis_config: RedisConfig


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    secret: str


@dataclass(frozen=True)
class DirectoryConfig:
    models: str
    working: str


@dataclass(frozen=True)
class ProcessingJobRequest:
    track_id: str
    youtube_url: str
    max_file_size_mb: int
    processing_timeout: int | None
    cache_key: str
    storage_config: StorageConfig
    webhook_config: WebhookConfig
    directory_config: DirectoryConfig
    cache_manager_config: AudioCacheConfig | None = None
    request_id: str = ""

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        validate_default=True,
        extra="ignore",
    )

    max_search_query_length: int = 200
    max_file_size_mb: int = 50

    max_workers: int = 1
    max_active_jobs: int = 1
    cleanup_interval: int = 3600
    processing_timeout: int = 60

    # Cache TTL values (in seconds)
    cache_ttl_seconds: int = 30 * 24 * 3600  # 30 days
    processing_ttl_seconds: int = 3600  # 1 hour
    cache_cleanup_ttl_seconds: int = 60

    # Webhook retry settings
    webhook_retry_max_attempts: int = 3
    webhook_retry_base_delay: int = 2

    # Health check rate limiting
    health_check_rate_limit_requests: int = 1000
    health_check_rate_limit_window: int = 60

    models_dir: str = "/tmp/audio-separator-models"
    working_dir: str = "audio_workspace"
    worker_name: str = ""
    worker_queue_names: str = "default"
    worker_concurrency: int = 1

    rate_limit_requests: str = "100 per hour"
    rate_limit_separation: str = "5 per minute"

    port: int = 5500
    debug: bool = False
    host: str = "0.0.0.0"

    webhook_secret: str = ""
    webhook_url: str = ""
    api_secret_key: str = ""

    # Cloudflare R2 for audio file storage
    cloudflare_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "audio-separation"
    r2_public_domain: str = ""

    # Redis for caching and rate limiting
    redis_url: str = ""

    @computed_field
    @property
    def r2_storage_enabled(self) -> bool:
        return bool(
            self.cloudflare_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_public_domain
        )

    @computed_field
    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    @computed_field
    @property
    def queue_names(self) -> list[str]:
        return [name.strip() for name in self.worker_queue_names.split(",") if name.strip()]

    def check_r2_environment_configs(self) -> None:
        missing_r2_configs = []

        if not self.cloudflare_account_id:
            missing_r2_configs.append("CLOUDFLARE_ACCOUNT_ID")
        if not self.r2_access_key_id:
            missing_r2_configs.append("R2_ACCESS_KEY_ID")
        if not self.r2_secret_access_key:
            missing_r2_configs.append("R2_SECRET_ACCESS_KEY")
        if not self.r2_public_domain:
            missing_r2_configs.append("R2_PUBLIC_DOMAIN")

        if missing_r2_configs:
            raise RuntimeError(
                "Missing required R2 environment variables: " + ", ".join(missing_r2_configs)
            )

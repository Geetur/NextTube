"""Shared configuration for all services, loaded from environment variables."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database — accepts both postgresql:// and postgresql+psycopg2:// forms
    database_url: str = "postgresql://postgres:postgres@postgres:5432/media"

    # Redis (available for caching)
    redis_url: str = "redis://redis:6379/0"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_topic: str = "media.transcode.requested"
    kafka_dead_letter_topic: str = "media.transcode.dlq"
    kafka_consumer_group: str = "worker-group"

    # Durable delivery and worker execution
    outbox_batch_size: int = 25
    outbox_poll_interval_seconds: float = 1.0
    outbox_claim_lease_seconds: int = 60
    outbox_publish_retry_delay_seconds: int = 5
    outbox_delivery_timeout_seconds: float = 10.0
    worker_lease_seconds: int = 900
    worker_max_retries: int = 3
    worker_retry_delays_raw: str = Field(
        default="30,120,600",
        validation_alias="WORKER_RETRY_DELAYS_SECONDS",
    )
    worker_max_poll_interval_ms: int = 3900000
    ffmpeg_timeout_seconds: int = 3600

    # Local Docker Compose autoscaling
    autoscaler_min_workers: int = 1
    autoscaler_max_workers: int = 3
    autoscaler_target_lag_per_worker: int = 1
    autoscaler_poll_interval_seconds: int = 10
    autoscaler_scale_up_cooldown_seconds: int = 15
    autoscaler_scale_down_cooldown_seconds: int = 300
    autoscaler_stop_timeout_seconds: int = 4200
    compose_project_name: str = "nexttube"

    # MinIO / S3
    s3_endpoint: str = "http://minio:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "media"

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_dsn(cls, v: str) -> str:
        """Strip the SQLAlchemy driver prefix so psycopg2 can use the URL directly."""
        if v.startswith("postgresql+psycopg2://"):
            return "postgresql://" + v.split("postgresql+psycopg2://", 1)[1]
        return v

    @field_validator("worker_retry_delays_raw")
    @classmethod
    def validate_retry_delays(cls, value: str) -> str:
        """Validate comma-separated retry delays from environment variables."""
        try:
            delays = tuple(int(delay.strip()) for delay in value.split(",") if delay.strip())
        except ValueError as exc:
            raise ValueError("retry delays must be comma-separated integers") from exc
        if not delays or any(delay <= 0 for delay in delays):
            raise ValueError("retry delays must contain positive integers")
        return value

    @property
    def worker_retry_delays_seconds(self) -> tuple[int, ...]:
        """Return validated retry delays as seconds."""
        return tuple(
            int(delay.strip())
            for delay in self.worker_retry_delays_raw.split(",")
            if delay.strip()
        )


settings = Settings()

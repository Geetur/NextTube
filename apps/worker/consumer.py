"""Kafka consumer loop: reads transcode jobs and orchestrates processing."""

import json
import pathlib
import shutil
import signal
import tempfile
import threading
import uuid

import boto3
from confluent_kafka import Consumer, KafkaError
from pydantic import BaseModel, ValidationError

import db as worker_db
from config import settings
from transcoder import build_master_playlist, transcode

_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}


class TranscodeRequested(BaseModel):
    """The versioned Kafka contract accepted by the worker."""

    event_id: str
    schema_version: int = 1
    delivery_attempt: int = 0
    job_id: str
    video_id: str
    profiles: list[int]


def _s3():
    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        endpoint_url=settings.s3_endpoint,
    )


def _download(s3_key: str, dest: pathlib.Path) -> None:
    print(f"[download] s3://{settings.s3_bucket}/{s3_key} -> {dest}")
    _s3().download_file(settings.s3_bucket, s3_key, str(dest))


def _upload_dir(
    local_dir: pathlib.Path,
    s3_prefix: str,
    heartbeat,
) -> None:
    client = _s3()
    paths = sorted(local_dir.glob("*"), key=lambda path: (path.name == "index.m3u8", path.name))
    for path in paths:
        if not heartbeat():
            raise RuntimeError("worker shutdown requested or job lease was lost")
        key = f"{s3_prefix}/{path.name}".replace("\\", "/")
        ct = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        client.upload_file(str(path), settings.s3_bucket, key, ExtraArgs={"ContentType": ct})
        print(f"[upload] {key}")


def _make_consumer() -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_consumer_group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "max.poll.interval.ms": settings.worker_max_poll_interval_ms,
        }
    )


def _process(event: TranscodeRequested, worker_id: str, stopping: threading.Event) -> None:
    claim = worker_db.claim_job(event.job_id, worker_id)
    if claim is None:
        print(f"[worker] event {event.event_id} is already handled or active")
        return

    print(
        f"[worker] job={claim.job_id} video={claim.video_id} "
        f"profiles={claim.profiles} attempt={claim.attempt}"
    )

    source_key = worker_db.fetch_source_key(claim.video_id)
    worker_db.ensure_renditions(claim)

    work_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"hls_{claim.video_id}_"))
    src_path = work_dir / "source.mp4"
    out_dir = work_dir / "out"

    def heartbeat() -> bool:
        return not stopping.is_set() and worker_db.refresh_lease(claim)

    try:
        _download(source_key, src_path)
        if not heartbeat():
            raise RuntimeError("worker shutdown requested or job lease was lost")
        results = transcode(src_path, out_dir, claim.profiles, heartbeat=heartbeat)
        if not all(results.values()):
            raise RuntimeError("FFmpeg did not create every requested rendition")

        s3_prefix = f"HLS/{claim.video_id}"
        (out_dir / "index.m3u8").write_text(
            build_master_playlist(claim.profiles), encoding="utf-8"
        )
        _upload_dir(out_dir, s3_prefix, heartbeat)

        worker_db.mark_job_done(claim, results, s3_prefix)
        print(f"[worker] job {claim.job_id} done.")
    except Exception as exc:
        print(f"[worker] job {claim.job_id} failed: {exc!r}")
        worker_db.retry_or_fail(claim, str(exc))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _worker_id() -> str:
    return f"{uuid.uuid4()}"


def run() -> None:
    """Start the Kafka consumer loop (blocking)."""
    consumer = _make_consumer()
    stopping = threading.Event()

    def stop(_signal, _frame) -> None:
        print("[worker] shutdown requested; finishing or retrying the active job")
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    consumer.subscribe([settings.kafka_topic])
    print(
        f"[worker] subscribed to {settings.kafka_topic!r} "
        f"as group {settings.kafka_consumer_group!r}"
    )

    try:
        while not stopping.is_set():
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    print(f"[worker] Kafka error: {msg.error()}")
                continue
            try:
                event = TranscodeRequested.model_validate_json(msg.value())
                if event.schema_version != 1:
                    raise ValueError(f"unsupported event schema {event.schema_version}")
            except (UnicodeDecodeError, ValidationError, ValueError) as exc:
                raw_event = msg.value().decode(errors="replace")
                worker_db.queue_malformed_event(raw_event, str(exc))
                print(f"[worker] moved malformed event to the DLQ: {exc}")
            else:
                _process(event, _worker_id(), stopping)
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()

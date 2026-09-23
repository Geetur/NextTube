"""Database state transitions owned by the transcode worker."""

from dataclasses import dataclass
import json
import uuid

import psycopg2

from config import settings


@dataclass(frozen=True)
class JobClaim:
    """The exclusive lease a worker holds while transcoding one video."""

    job_id: str
    video_id: str
    profiles: list[int]
    worker_id: str
    attempt: int


def _connect():
    return psycopg2.connect(settings.database_url)


def _decode_payload(payload: dict | str | None) -> dict:
    if isinstance(payload, str):
        return json.loads(payload)
    return payload or {}


def _insert_outbox_event(
    cur,
    *,
    job_id: str | None,
    topic: str,
    message_key: str,
    payload: dict,
    delay_seconds: int = 0,
) -> None:
    cur.execute(
        "INSERT INTO outbox_events("
        "id, job_id, topic, message_key, payload, state, available_at"
        ") VALUES ("
        "%s, %s, %s, %s, %s, 'pending', NOW() + (%s * INTERVAL '1 second')"
        ")",
        (
            str(uuid.uuid4()),
            job_id,
            topic,
            message_key,
            json.dumps(payload),
            delay_seconds,
        ),
    )


def _dead_letter_payload(
    job_id: str | None,
    video_id: str | None,
    profiles: list[int] | None,
    attempt: int | None,
    error: str,
    raw_event: str | None = None,
) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "schema_version": 1,
        "event_type": "media.transcode.failed",
        "job_id": job_id,
        "video_id": video_id,
        "profiles": profiles or [],
        "attempt": attempt,
        "error": error[:4000],
    }
    if raw_event is not None:
        payload["raw_event"] = raw_event
    return payload


def claim_job(job_id: str, worker_id: str) -> JobClaim | None:
    """Claim queued work or a stale lease, returning no claim for duplicates."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT video_id, payload, status, attempt, lease_expires_at "
            "FROM jobs WHERE id=%s FOR UPDATE",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        video_id, payload, status, attempt, lease_expires_at = row
        if status in {"done", "failed"}:
            return None
        if status == "running" and lease_expires_at is not None:
            cur.execute("SELECT NOW()")
            if lease_expires_at > cur.fetchone()[0]:
                return None
        if status not in {"queued", "retry_wait", "running"}:
            raise RuntimeError(f"job {job_id!r} has unsupported status {status!r}")

        job_payload = _decode_payload(payload)
        profiles = [int(height) for height in job_payload.get("profiles", [240, 480, 720])]
        if attempt > settings.worker_max_retries:
            _insert_outbox_event(
                cur,
                job_id=job_id,
                topic=settings.kafka_dead_letter_topic,
                message_key=job_id,
                payload=_dead_letter_payload(
                    job_id,
                    video_id,
                    profiles,
                    attempt,
                    "worker lease expired after the retry budget was exhausted",
                ),
            )
            cur.execute(
                "UPDATE jobs SET status='failed', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL, finished_at=NOW(), updated_at=NOW() WHERE id=%s",
                ("worker lease expired after the retry budget was exhausted", job_id),
            )
            return None

        next_attempt = attempt + 1
        cur.execute(
            "UPDATE jobs SET status='running', attempt=%s, lease_owner=%s, "
            "lease_expires_at=NOW() + (%s * INTERVAL '1 second'), "
            "started_at=COALESCE(started_at, NOW()), updated_at=NOW() WHERE id=%s",
            (next_attempt, worker_id, settings.worker_lease_seconds, job_id),
        )

    return JobClaim(job_id, video_id, profiles, worker_id, next_attempt)


def refresh_lease(claim: JobClaim) -> bool:
    """Extend a running claim so another worker cannot resume it."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET lease_expires_at=NOW() + (%s * INTERVAL '1 second'), "
            "updated_at=NOW() WHERE id=%s AND status='running' AND lease_owner=%s",
            (settings.worker_lease_seconds, claim.job_id, claim.worker_id),
        )
        return cur.rowcount == 1


def fetch_source_key(video_id: str) -> str:
    """Return the S3 key for the source video, raising if not found."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT key FROM videos WHERE id=%s", (video_id,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"video {video_id!r} not found in database")
    return row[0]


def ensure_renditions(claim: JobClaim) -> None:
    """Upsert rendition rows for the claimed job and set them running."""
    with _connect() as conn, conn.cursor() as cur:
        for height in claim.profiles:
            cur.execute(
                "INSERT INTO renditions(id, video_id, height, status) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (str(uuid.uuid4()), claim.video_id, height, "queued"),
            )
            cur.execute(
                "UPDATE renditions SET status='running' WHERE video_id=%s AND height=%s",
                (claim.video_id, height),
            )


def mark_job_done(claim: JobClaim, results: dict[int, bool], s3_prefix: str) -> None:
    """Publish the completed rendition state and release the job lease atomically."""
    with _connect() as conn, conn.cursor() as cur:
        for height, ok in results.items():
            status = "ready" if ok else "failed"
            key = f"{s3_prefix}/{height}.m3u8"
            cur.execute(
                "UPDATE renditions SET status=%s, key=%s WHERE video_id=%s AND height=%s",
                (status, key, claim.video_id, height),
            )
        cur.execute(
            "UPDATE jobs SET status='done', lease_owner=NULL, lease_expires_at=NULL, "
            "finished_at=NOW(), updated_at=NOW() "
            "WHERE id=%s AND status='running' AND lease_owner=%s",
            (claim.job_id, claim.worker_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"job {claim.job_id!r} lease was lost before completion")


def retry_or_fail(claim: JobClaim, error: str) -> None:
    """Schedule the next retry or publish a terminal failure to the DLQ."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT attempt FROM jobs WHERE id=%s AND status='running' AND lease_owner=%s "
            "FOR UPDATE",
            (claim.job_id, claim.worker_id),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"job {claim.job_id!r} lease was lost before failure handling")

        attempt = row[0]
        if attempt <= settings.worker_max_retries:
            delay_index = min(attempt - 1, len(settings.worker_retry_delays_seconds) - 1)
            delay_seconds = settings.worker_retry_delays_seconds[delay_index]
            _insert_outbox_event(
                cur,
                job_id=claim.job_id,
                topic=settings.kafka_topic,
                message_key=claim.job_id,
                payload={
                    "event_id": str(uuid.uuid4()),
                    "schema_version": 1,
                    "delivery_attempt": attempt,
                    "job_id": claim.job_id,
                    "video_id": claim.video_id,
                    "profiles": claim.profiles,
                },
                delay_seconds=delay_seconds,
            )
            cur.execute(
                "UPDATE jobs SET status='retry_wait', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL, updated_at=NOW() WHERE id=%s",
                (error[:4000], claim.job_id),
            )
            return

        _insert_outbox_event(
            cur,
            job_id=claim.job_id,
            topic=settings.kafka_dead_letter_topic,
            message_key=claim.job_id,
            payload=_dead_letter_payload(
                claim.job_id,
                claim.video_id,
                claim.profiles,
                attempt,
                error,
            ),
        )
        cur.execute(
            "UPDATE jobs SET status='failed', last_error=%s, lease_owner=NULL, "
            "lease_expires_at=NULL, finished_at=NOW(), updated_at=NOW() WHERE id=%s",
            (error[:4000], claim.job_id),
        )


def queue_malformed_event(raw_event: str, error: str) -> None:
    """Persist a malformed Kafka payload for delivery to the DLQ."""
    with _connect() as conn, conn.cursor() as cur:
        _insert_outbox_event(
            cur,
            job_id=None,
            topic=settings.kafka_dead_letter_topic,
            message_key="invalid-event",
            payload=_dead_letter_payload(None, None, None, None, error, raw_event),
        )

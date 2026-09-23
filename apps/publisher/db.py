"""Transactional outbox database operations for the publisher."""

from dataclasses import dataclass
import json

import psycopg2

from config import settings


@dataclass(frozen=True)
class OutboxEvent:
    """A database-backed event awaiting broker delivery."""

    id: str
    topic: str
    message_key: str
    payload: dict


def _connect():
    return psycopg2.connect(settings.database_url)


def claim_events(publisher_id: str) -> list[OutboxEvent]:
    """Claim a batch of due events without overlapping another publisher."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE outbox_events "
            "SET state='pending', claimed_by=NULL, claim_expires_at=NULL, updated_at=NOW() "
            "WHERE state='publishing' AND claim_expires_at < NOW()"
        )
        cur.execute(
            "WITH candidates AS ("
            "  SELECT id FROM outbox_events "
            "  WHERE state='pending' AND available_at <= NOW() "
            "  ORDER BY available_at, created_at "
            "  FOR UPDATE SKIP LOCKED "
            "  LIMIT %s"
            ") "
            "UPDATE outbox_events AS event "
            "SET state='publishing', claimed_by=%s, "
            "    claim_expires_at=NOW() + (%s * INTERVAL '1 second'), "
            "    publish_attempts=event.publish_attempts + 1, updated_at=NOW() "
            "FROM candidates "
            "WHERE event.id=candidates.id "
            "RETURNING event.id, event.topic, event.message_key, event.payload",
            (
                settings.outbox_batch_size,
                publisher_id,
                settings.outbox_claim_lease_seconds,
            ),
        )
        rows = cur.fetchall()

    events: list[OutboxEvent] = []
    for event_id, topic, message_key, payload in rows:
        decoded_payload = json.loads(payload) if isinstance(payload, str) else payload
        events.append(
            OutboxEvent(
                id=event_id,
                topic=topic,
                message_key=message_key,
                payload=decoded_payload,
            )
        )
    return events


def mark_published(event_id: str, publisher_id: str) -> None:
    """Record a broker-acknowledged event as delivered."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE outbox_events "
            "SET state='published', published_at=NOW(), claim_expires_at=NULL, updated_at=NOW() "
            "WHERE id=%s AND state='publishing' AND claimed_by=%s",
            (event_id, publisher_id),
        )


def release_event(event_id: str, publisher_id: str, error: str) -> None:
    """Return a failed publication to the due queue after a short delay."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE outbox_events "
            "SET state='pending', claimed_by=NULL, claim_expires_at=NULL, "
            "    available_at=NOW() + (%s * INTERVAL '1 second'), "
            "    last_error=%s, updated_at=NOW() "
            "WHERE id=%s AND state='publishing' AND claimed_by=%s",
            (
                settings.outbox_publish_retry_delay_seconds,
                error[:4000],
                event_id,
                publisher_id,
            ),
        )
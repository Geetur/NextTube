"""Publish database outbox events to Kafka with broker acknowledgement."""

import os
import socket
import time

from confluent_kafka import Producer

import db
from config import settings


def _publisher_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _publish_batch(producer: Producer, publisher_id: str, events: list[db.OutboxEvent]) -> None:
    results: dict[str, str | None] = {}

    for event in events:
        def on_delivery(error, _message, event_id: str = event.id) -> None:
            results[event_id] = str(error) if error else None

        try:
            producer.produce(
                event.topic,
                key=event.message_key.encode(),
                value=json_dumps(event.payload),
                on_delivery=on_delivery,
            )
        except BufferError as exc:
            results[event.id] = str(exc)

    producer.flush(settings.outbox_delivery_timeout_seconds)

    for event in events:
        result = results.get(event.id, "broker acknowledgement timed out")
        if result is None:
            db.mark_published(event.id, publisher_id)
            print(f"[publisher] delivered event {event.id}")
        else:
            db.release_event(event.id, publisher_id, result)
            print(f"[publisher] deferred event {event.id}: {result}")


def json_dumps(payload: dict) -> bytes:
    """Encode the database JSON payload exactly once for Kafka."""
    import json

    return json.dumps(payload, separators=(",", ":")).encode()


def run() -> None:
    """Claim and deliver outbox events until the process is stopped."""
    publisher_id = _publisher_id()
    producer = Producer({"bootstrap.servers": settings.kafka_bootstrap_servers})
    print(f"[publisher] started as {publisher_id}")

    try:
        while True:
            events = db.claim_events(publisher_id)
            if not events:
                time.sleep(settings.outbox_poll_interval_seconds)
                continue
            _publish_batch(producer, publisher_id, events)
    except KeyboardInterrupt:
        print("[publisher] stopping")
    finally:
        producer.flush(settings.outbox_delivery_timeout_seconds)


if __name__ == "__main__":
    run()
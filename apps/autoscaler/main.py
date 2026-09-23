"""Scale local Docker Compose workers from Kafka consumer lag."""

import math
import time

import docker
from confluent_kafka import Consumer, TopicPartition

from config import settings


def consumer_lag() -> int:
    """Return total uncommitted offsets for the transcode worker group."""
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_consumer_group,
            "enable.auto.commit": False,
        }
    )
    try:
        metadata = consumer.list_topics(settings.kafka_topic, timeout=10)
        topic = metadata.topics.get(settings.kafka_topic)
        if topic is None or topic.error is not None:
            raise RuntimeError(f"Kafka topic {settings.kafka_topic!r} is unavailable")

        partitions = [
            TopicPartition(settings.kafka_topic, partition_id)
            for partition_id in topic.partitions
        ]
        committed_offsets = consumer.committed(partitions, timeout=10)
        total = 0
        for partition, committed in zip(partitions, committed_offsets, strict=True):
            low, high = consumer.get_watermark_offsets(partition, timeout=10)
            offset = high if committed.offset < 0 else max(committed.offset, low)
            total += max(high - offset, 0)
        return total
    finally:
        consumer.close()


def desired_replicas(lag: int) -> int:
    """Bound consumer capacity by the configured local worker limit."""
    required = max(1, math.ceil(lag / settings.autoscaler_target_lag_per_worker))
    return min(
        max(required, settings.autoscaler_min_workers),
        settings.autoscaler_max_workers,
    )


def should_scale_up(current: int, desired: int, now: float, last_scale_at: float) -> bool:
    """Allow responsive growth while avoiding repeated Docker API calls."""
    return (
        desired > current
        and now - last_scale_at >= settings.autoscaler_scale_up_cooldown_seconds
    )


def should_scale_down(
    lag: int,
    current: int,
    desired: int,
    now: float,
    last_nonzero_lag_at: float,
) -> bool:
    """Shrink only after the queue has stayed empty for the full cooldown."""
    return (
        desired < current
        and lag == 0
        and now - last_nonzero_lag_at >= settings.autoscaler_scale_down_cooldown_seconds
    )


def worker_containers(client: docker.DockerClient) -> list:
    """Return running workers created by the local Compose project."""
    return client.containers.list(
        filters={
            "label": [
                f"com.docker.compose.project={settings.compose_project_name}",
                "com.docker.compose.service=worker",
            ]
        }
    )


def _container_number(container) -> int:
    labels = container.labels or {}
    return int(labels.get("com.docker.compose.container-number", "0"))


def _clone_worker(client: docker.DockerClient, source, number: int) -> None:
    """Create a worker from Compose's container config and host-valid mounts."""
    source.reload()
    config = source.attrs["Config"]
    labels = dict(config.get("Labels") or {})
    labels["com.docker.compose.container-number"] = str(number)

    network_config = {}
    for name, endpoint in source.attrs["NetworkSettings"]["Networks"].items():
        aliases = endpoint.get("Aliases")
        kwargs = {"aliases": aliases} if aliases else {}
        network_config[name] = client.api.create_endpoint_config(**kwargs)

    created = client.api.create_container(
        image=config["Image"],
        name=f"{settings.compose_project_name}-worker-{number}",
        hostname=config.get("Hostname") or None,
        user=config.get("User") or None,
        environment=config.get("Env"),
        command=config.get("Cmd"),
        working_dir=config.get("WorkingDir") or None,
        labels=labels,
        entrypoint=config.get("Entrypoint") or None,
        host_config=source.attrs["HostConfig"],
        networking_config=client.api.create_networking_config(network_config),
    )
    client.api.start(created["Id"])
    print(f"[autoscaler] started worker {number}")


def scale_workers(client: docker.DockerClient, workers: list, replicas: int) -> None:
    """Create or stop workers while preserving Compose's worker configuration."""
    current = len(workers)
    if replicas > current:
        if not workers:
            raise RuntimeError("cannot scale up without a Compose-managed worker template")
        next_number = max(_container_number(worker) for worker in workers) + 1
        for number in range(next_number, next_number + replicas - current):
            _clone_worker(client, workers[0], number)
    elif replicas < current:
        for worker in sorted(workers, key=_container_number, reverse=True)[: current - replicas]:
            print(f"[autoscaler] stopping worker {worker.name}")
            worker.stop(timeout=settings.autoscaler_stop_timeout_seconds)
            worker.remove()


def run() -> None:
    """Continuously reconcile worker replicas using lag and conservative cooldowns."""
    last_scale_at = 0.0
    last_nonzero_lag_at = time.monotonic()
    client = docker.from_env()
    print(
        f"[autoscaler] managing {settings.kafka_topic!r} from "
        f"{settings.autoscaler_min_workers} to {settings.autoscaler_max_workers} workers"
    )

    while True:
        try:
            lag = consumer_lag()
            now = time.monotonic()
            if lag > 0:
                last_nonzero_lag_at = now

            workers = worker_containers(client)
            current = len(workers)
            desired = desired_replicas(lag)
            if should_scale_up(current, desired, now, last_scale_at):
                scale_workers(client, workers, desired)
                last_scale_at = now
            elif should_scale_down(lag, current, desired, now, last_nonzero_lag_at):
                scale_workers(client, workers, desired)
                last_scale_at = now
            else:
                print(f"[autoscaler] lag={lag} workers={current} desired={desired}")
        except Exception as exc:
            print(f"[autoscaler] reconciliation failed: {exc!r}")

        time.sleep(settings.autoscaler_poll_interval_seconds)


if __name__ == "__main__":
    run()
# CLAUDE.md — codebase guide for AI agents

## What this project is
NextTube: video upload → Kafka → Python worker → FFmpeg HLS transcode → MinIO → hls.js playback.
Stack: Next.js 14 · FastAPI · Postgres · Kafka (KRaft) · Redis · MinIO · Docker Compose.

---

## File map

```
infra/docker-compose.yml        local services, publisher, and autoscaler

apps/config.py                  pydantic-settings — single source of env vars

apps/api/app/
  db.py                         SQLAlchemy engine only
  s3.py                         boto3 MinIO helpers (client + put_bytes)
  main.py                       ALL routes + Pydantic models inline

apps/worker/
  transcoder.py                 FFmpeg logic — transcode() + build_master_playlist()
  db.py                         DB claims, leases, retries, and final state
  consumer.py                   Kafka consumer loop + S3 download/upload helpers
  main.py                       entry point — calls consumer.run()

apps/publisher/
  db.py                         transactional outbox claims and acknowledgements
  main.py                       Kafka publishing loop

apps/autoscaler/
  main.py                       local Kafka-lag worker controller
```

---

## Architecture decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Kafka image | bitnamilegacy/kafka:3.7 | KRaft mode (no ZooKeeper); `bitnami/kafka:3.7` is no longer published |
| Kafka Python lib | confluent-kafka | Pre-built wheels, production-grade, no librdkafka build needed on python:3.11-slim |
| Topic | `media.transcode.requested` | 3 partitions, RF=1 (local dev only) |
| Dead-letter topic | `media.transcode.dlq` | Terminal failures and malformed events |
| Consumer group | `worker-group` | |
| Kafka UI | provectuslabs/kafka-ui on :8080 | Free dev tooling |
| Config | pydantic-settings BaseSettings in both api and worker | One import, type-safe, .env auto-loaded |
| Worker DSN | Pydantic `field_validator` normalises `postgresql+psycopg2://` → `postgresql://` | psycopg2 direct needs plain URL |
| Schemas | Pydantic models inline in `main.py` (not a separate file) | Simpler — only one consumer of these models |
| Redis | Kept as a service, not removed | Available for future caching; no longer the job queue |

---

## Kafka topic flow

```
POST /jobs/transcode
  → insert jobs row + outbox event (one transaction)

publisher.py:run()
  → claim due outbox event
  → Producer.produce("media.transcode.requested", job_id, payload_json)
  → broker acknowledgement → mark outbox event published

consumer.py:run()
  → Consumer.poll()
  → claim_job() and lease
  → _process(job)
      → ensure_renditions (db.py)
      → _download (S3 → /tmp)
      → transcode (transcoder.py)
      → _upload_dir (/tmp → S3)
      → mark_job_done or retry_or_fail (db.py)
  → commit Kafka offset after durable outcome
```

---

## Guidelines

- **Keep modules flat.** Don't create new files unless a module has a clearly separable, reusable concern. `consumer.py` owns S3 I/O specific to the worker; `transcoder.py` owns FFmpeg; `db.py` owns SQL.
- **No ORM in worker.** Worker uses raw psycopg2 to stay dependency-light; API uses SQLAlchemy.
- **All config via pydantic-settings.** Never use `os.getenv` directly — import `settings` from `config.py`.
- **Idiomatic Python.** Module-level docstrings on all files. No inline comments that restate what the code does. Use `f""` not `%` or `.format()`. Type-hint public functions.
- **Don't add abstractions for single use cases.** A helper only earns its own module when more than one other module imports it.
- **Kafka message format** is versioned `TranscodeJobPayload` JSON: `{event_id, schema_version, delivery_attempt, job_id, video_id, profiles}`. Keep this stable — it is the contract between publisher and worker.
- **Delivery is at-least-once.** The transactional outbox may redeliver after an acknowledgement crash; worker claims make active and completed jobs duplicate-safe.
- **Worker retry is bounded.** The supplied delays are 30 seconds, 2 minutes, and 10 minutes; exhausted or malformed events go to `media.transcode.dlq`.
- **Local autoscaling is privileged.** The autoscaler uses the Docker socket and is restricted to one to three workers, matching request-topic partitions.
- **No breaking changes to the DB schema** without a new Alembic migration.

---

## Service ports (quick ref)

| Port | Service |
|------|---------|
| 3000 | Next.js web |
| 8000 | FastAPI |
| 8080 | kafka-ui |
| 9000 | MinIO API |
| 9001 | MinIO Console |
| 9092 | Kafka broker |
| 5432 | Postgres |
| 6379 | Redis |

---

## Changes log

### 2026-04-30 — Kafka refactor + general cleanup
- Replaced Redis job queue (`BRPOP`/`LPUSH`) with Kafka topic `media.transcode.requested`
- Redis service kept (available for caching)
- Added `kafka`, `kafka-init`, `kafka-ui` to docker-compose
- Consolidated two per-app `config.py` files into a single `apps/config.py`; mounted into both containers via docker-compose volume entries
- Split monolithic worker into: `transcoder.py`, `db.py`, `consumer.py`, `main.py`
- Folded schemas into `main.py` (removed `schemas.py`)
- Removed `downloader.py` and `uploader.py` — S3 I/O lives in `consumer.py`
- Updated `api/requirements.txt`: added `confluent-kafka`, `pydantic-settings`; removed none
- Updated `worker/requirements.txt`: replaced `redis` with `confluent-kafka`, added `pydantic-settings`
- Rewrote README

### 2026-09-23 — Durable Kafka delivery and local scaling
- Added transactional outbox events so job creation does not directly depend on API-process Kafka publication
- Added publisher service that waits for broker acknowledgements before marking events delivered
- Added worker leases, manual offset commits, bounded retries, FFmpeg timeout, and dead-letter delivery
- Added local Docker-socket autoscaler driven by Kafka consumer lag, capped at the three request-topic partitions

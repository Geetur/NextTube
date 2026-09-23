"""Media Optimizer API."""

import json
import mimetypes
import uuid
from urllib.parse import urlencode

import redis
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.s3 import client as get_s3, put_bytes


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    video_id: str
    key: str

class TranscodeRequest(BaseModel):
    video_id: str
    profiles: list[int] | None = None

class TranscodeJobPayload(BaseModel):
    event_id: str
    schema_version: int = 1
    delivery_attempt: int = 0
    job_id: str
    video_id: str
    profiles: list[int]

class JobResponse(BaseModel):
    job_id: str
    status: str

class JobDetail(BaseModel):
    job_id: str
    status: str
    type: str
    payload: dict
    created_at: str
    updated_at: str

class RenditionOut(BaseModel):
    height: int
    status: str
    key: str | None

class VideoSummary(BaseModel):
    id: str
    source_key: str
    created_at: str
    renditions: list[RenditionOut]

class VideoListItem(BaseModel):
    id: str
    key: str
    created_at: str

# Redis is kept available for caching; it is no longer the job queue.
_redis = redis.Redis.from_url(settings.redis_url)

app = FastAPI(title="Media Optimizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    """Liveness probe that checks Postgres and Redis connectivity."""
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    redis_ok = True
    try:
        _redis.ping()
    except Exception:
        redis_ok = False

    return {"ok": True, "db": db_ok, "redis": redis_ok}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@app.post("/upload", response_model=UploadResponse)
async def upload(file: UploadFile = File(...)):
    """Upload a source video to MinIO and register it in the database."""
    fname = (file.filename or "").lower()
    ext = ("." + fname.split(".")[-1]) if "." in fname else ".mp4"
    vid = str(uuid.uuid4())
    key = f"source/{vid}{ext}"

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    put_bytes(key, data, content_type=file.content_type or "application/octet-stream")

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO videos(id, key) VALUES (:id, :key)"),
            {"id": vid, "key": key},
        )

    return UploadResponse(video_id=vid, key=key)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@app.post("/jobs/transcode", response_model=JobResponse)
def make_job(body: TranscodeRequest):
    """Create a transcode job and persist an event for reliable publication."""
    job_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    profiles = body.profiles or [240, 480, 720]
    message = TranscodeJobPayload(
        event_id=event_id,
        job_id=job_id,
        video_id=body.video_id,
        profiles=profiles,
    )

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM videos WHERE id=:id"), {"id": body.video_id}
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="video not found")

        conn.execute(
            text(
                "INSERT INTO jobs(id, video_id, type, payload, status) "
                "VALUES (:id, :vid, 'transcode', :payload, 'queued')"
            ),
            {"id": job_id, "vid": body.video_id, "payload": json.dumps({"profiles": profiles})},
        )
        conn.execute(
            text(
                "INSERT INTO outbox_events("
                "id, job_id, topic, message_key, payload, state"
                ") VALUES ("
                ":id, :job_id, :topic, :message_key, :payload, 'pending'"
                ")"
            ),
            {
                "id": event_id,
                "job_id": job_id,
                "topic": settings.kafka_topic,
                "message_key": job_id,
                "payload": message.model_dump_json(),
            },
        )

    return JobResponse(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}", response_model=JobDetail)
def job_status(job_id: str):
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT id, status, type, payload, created_at, updated_at "
                "FROM jobs WHERE id=:id"
            ),
            {"id": job_id},
        ).first()

    if not row:
        raise HTTPException(status_code=404, detail="job not found")

    return JobDetail(
        job_id=row[0],
        status=row[1],
        type=row[2],
        payload=row[3],
        created_at=str(row[4]),
        updated_at=str(row[5]),
    )


# ---------------------------------------------------------------------------
# HLS proxy (API → MinIO) — avoids CORS issues in the browser
# ---------------------------------------------------------------------------


def _content_type_for(key: str) -> str:
    if key.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if key.endswith(".m4s"):
        return "video/iso.segment"
    guess, _ = mimetypes.guess_type(key)
    return guess or "application/octet-stream"


@app.get("/videos/{video_id}/basic")
def serve_basic_stream(video_id: str):
    """Stream the original source video (MP4) directly from MinIO."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT key FROM videos WHERE id=:id"),
            {"id": video_id},
        ).first()

    if not row:
        raise HTTPException(status_code=404, detail="video not found")

    obj = get_s3().get_object(Bucket=settings.s3_bucket, Key=row[0])
    return StreamingResponse(
        obj["Body"],
        media_type=_content_type_for(row[0]),
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.get("/videos/{video_id}/summary", response_model=VideoSummary)
def video_summary(video_id: str):
    with engine.begin() as conn:
        v = conn.execute(
            text("SELECT id, key, created_at FROM videos WHERE id=:id"),
            {"id": video_id},
        ).first()
        if not v:
            raise HTTPException(status_code=404, detail="video not found")

        renditions = conn.execute(
            text(
                "SELECT height, status, key FROM renditions "
                "WHERE video_id=:id ORDER BY height"
            ),
            {"id": video_id},
        ).fetchall()

    return VideoSummary(
        id=v[0],
        source_key=v[1],
        created_at=str(v[2]),
        renditions=[{"height": r[0], "status": r[1], "key": r[2]} for r in renditions],
    )


@app.get("/videos/{video_id}/playlist")
def serve_master_playlist(video_id: str, v: str | None = None):
    obj = get_s3().get_object(Bucket=settings.s3_bucket, Key=f"HLS/{video_id}/index.m3u8")
    content = obj["Body"].read().decode("utf-8")
    if v:
        query = urlencode({"v": v})
        content = "\n".join(
            f"{line}?{query}" if line.endswith(".m3u8") else line
            for line in content.splitlines()
        ) + "\n"
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/videos/{video_id}/{path:path}")
def serve_hls_child(video_id: str, path: str, v: str | None = None):
    key = f"HLS/{video_id}/{path}"
    obj = get_s3().get_object(Bucket=settings.s3_bucket, Key=key)
    if key.endswith(".m3u8"):
        content = obj["Body"].read().decode("utf-8")
        if v:
            query = urlencode({"v": v})
            lines: list[str] = []
            for line in content.splitlines():
                if line.startswith('#EXT-X-MAP:URI="') and line.endswith('"'):
                    uri = line.removeprefix('#EXT-X-MAP:URI="').removesuffix('"')
                    lines.append(f'#EXT-X-MAP:URI="{uri}?{query}"')
                elif line and not line.startswith("#"):
                    lines.append(f"{line}?{query}")
                else:
                    lines.append(line)
            content = "\n".join(lines) + "\n"
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    return StreamingResponse(
        obj["Body"],
        media_type=_content_type_for(key),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/videos", response_model=list[VideoListItem])
def list_videos(limit: int = 25):
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, key, created_at FROM videos "
                "ORDER BY created_at DESC LIMIT :lim"
            ),
            {"lim": limit},
        ).fetchall()
    return [VideoListItem(id=r[0], key=r[1], created_at=str(r[2])) for r in rows]


# ---------------------------------------------------------------------------
# Metrics player
# ---------------------------------------------------------------------------


@app.get("/metrics-player")
def metrics_player():
    """
    Serve an HTML page that plays a video and exposes playback metrics on
    ``window.__metrics__``: startupTime, stallCount, stallTime, currentTime.

    Query params:
      - ``video_id``: ID of the video to play (required)
      - ``mode``: ``"abr"`` (default, HLS via hls.js) or ``"basic"`` (MP4)
    """
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Metrics Player</title>
      </head>
      <body>
        <h3>Metrics Player</h3>
        <video id="video" controls width="640" height="360"></video>

        <pre id="metrics-display"></pre>

        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script>
          const params = new URLSearchParams(window.location.search);
          const mode = params.get("mode") || "abr";
          const videoId = params.get("video_id");

          const video = document.getElementById("video");
          const metricsEl = document.getElementById("metrics-display");

          let stallCount = 0;
          let stallTime = 0;
          let stallStart = null;
          let startupTime = null;
          const startTime = performance.now();

          function updateMetrics() {
            window.__metrics__ = {
              stallCount,
              stallTime,
              startupTime,
              currentTime: video.currentTime || 0
            };
            if (metricsEl) {
              metricsEl.textContent = JSON.stringify(window.__metrics__, null, 2);
            }
          }

          video.addEventListener("waiting", () => {
            stallCount += 1;
            stallStart = performance.now();
            updateMetrics();
          });

          video.addEventListener("playing", () => {
            if (startupTime === null) {
              startupTime = (performance.now() - startTime) / 1000.0;
            }
            if (stallStart !== null) {
              stallTime += (performance.now() - stallStart) / 1000.0;
              stallStart = null;
            }
            updateMetrics();
          });

          updateMetrics();
          setInterval(updateMetrics, 1000);

          if (!videoId) {
            metricsEl.textContent = "Missing ?video_id=...";
          } else {
              const src = mode === "abr"
                  ? `/videos/${videoId}/playlist?v=${Date.now()}`
              : `/videos/${videoId}/basic`;

            if (mode === "abr" && window.Hls && Hls.isSupported()) {
              const hls = new Hls();
              hls.loadSource(src);
              hls.attachMedia(video);
            } else {
              video.src = src;
            }

            video.muted = true;
            video.play().catch(() => {});
          }
        </script>
      </body>
    </html>
    """
    return Response(content=html, media_type="text/html")

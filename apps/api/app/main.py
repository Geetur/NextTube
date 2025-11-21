# apps/api/app/main.py

import os, uuid, json, mimetypes
import redis
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text

from app.db import engine
from app.s3 import put_bytes
from app.s3 import client as get_s3  # alias the s3 client

# ------------------------------------------------------------------
# Globals
# ------------------------------------------------------------------
S3_BUCKET = os.getenv("S3_BUCKET", "media")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.Redis.from_url(REDIS_URL)

app = FastAPI(title="Media Optimizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Health endpoint
# ------------------------------------------------------------------
# extendable with checks to other dependencies such as s3/minio, migrations, etc
@app.get("/healthz")
def healthz():
    # its important that we don't just return 200
    # if the app starts, we want to see if the dependencies
    # themselves are reachable
    db_ok = True
    # checking postgreswith a simple select
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    red_ok = True
    # testing redis with a ping
    try:
        r.ping()
    except Exception:
        red_ok = False
    # app at this point is either running with working dependencies
    # or not
    return {"ok": True, "db": db_ok, "redis": red_ok}

# ------------------------------------------------------------------
# Upload endpoint
# ------------------------------------------------------------------
# the basemodel is slightly unecessary but is good practive
class UploadResp(BaseModel):
    video_id: str
    key: str

# were uploading a file to minio and updating postgres status
@app.post("/upload", response_model=UploadResp)
# getting the file via stream through fastapis UploadFile
async def upload(file: UploadFile = File(...)):
    fname = (file.filename or "").lower()
    #default to mp4 if extension can't be determined by mp4
    ext = "." + fname.split(".")[-1] if "." in fname else ".mp4"
    # generating video id and s3 key

    # we actually key by uuid to avoid collisions if two files have the same
    # video id
    vid = str(uuid.uuid4())
    key = f"source/{vid}{ext}"

    data = await file.read()
    #ideally there is some max-size check and other sorts of content validation
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    # Save to MinIO
    put_bytes(key, data, content_type=file.content_type or "application/octet-stream")

    # Record in DB so downstream jobs can find the raw video object
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO videos(id, key) VALUES (:id,:key)"), {"id": vid, "key": key})

    # returning the video id (uuid) and key (s3) as a pydantic model
    return UploadResp(video_id=vid, key=key)

# ------------------------------------------------------------------
# Jobs endpoint
# ------------------------------------------------------------------
class JobCreate(BaseModel):
    video_id: str
    profiles: list[int] | None = None   # e.g. [240, 480, 720]

class JobResp(BaseModel):
    job_id: str
    status: str

# were creating jobs here. think of it as a job queue + state machine
@app.post("/jobs/transcode", response_model=JobResp)
def make_job(body: JobCreate):
    with engine.begin() as conn:
        # check that video exists in database
        row = conn.execute(text("SELECT 1 FROM videos WHERE id=:id"), {"id": body.video_id}).first()
        if not row:
            raise HTTPException(status_code=404, detail="video not found")
        # again were using uuid4 to create the unique job id here to avoid collisions
        job_id = str(uuid.uuid4())
        # we use the specified profile or default to 240, 480, 720 for simplicty sake
        payload = {"profiles": body.profiles or [240, 480, 720]}
        # insert into job with the key being the uuid, video being the video id, and the payload being
        # the json(profiles). this allows for the job state to be tracked alongisde actually being worked on
        conn.execute(
            text("INSERT INTO jobs(id, video_id, type, payload, status) "
                 "VALUES (:id,:vid,'transcode',:payload,'queued')"),
            {"id": job_id, "vid": body.video_id, "payload": json.dumps(payload)},
        )

    # Enqueue for worker. left push a json job into the redis list with the same form as the postgres
    # insertion
    r.lpush("jobs:transcode", json.dumps({"job_id": job_id, "video_id": body.video_id, **payload}))

    # return a pydantic model
    return JobResp(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id, status, type, payload, created_at, updated_at "
            "FROM jobs WHERE id=:id"
        ), {"id": job_id}).first()

        if not row:
            raise HTTPException(status_code=404, detail="job not found")

        return {
            "job_id": row[0],
            "status": row[1],
            "type": row[2],
            "payload": row[3],
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }

# ------------------------------------------------------------------
# HLS proxy (API → MinIO) to avoid CORS
# ------------------------------------------------------------------
def _content_type_for(key: str) -> str:
    if key.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if key.endswith(".ts"):
        return "video/MP2T"
    guess, _ = mimetypes.guess_type(key)
    return guess or "application/octet-stream"

@app.get("/videos/{video_id}/playlist")
def serve_master_playlist(video_id: str):
    key = f"HLS/{video_id}/index.m3u8"
    s3 = get_s3()
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    data = obj["Body"].read()
    return Response(
        content=data,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "public, max-age=60"},
    )

@app.get("/videos/{video_id}/{path:path}")
def serve_hls_child(video_id: str, path: str):
    # e.g. 240.m3u8, 240_000.ts, etc.
    key = f"HLS/{video_id}/{path}"
    s3 = get_s3()
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return StreamingResponse(
        obj["Body"],
        media_type=_content_type_for(key),
        headers={"Cache-Control": "public, max-age=3600"},
    )

# --- Videos: list recent ---
@app.get("/videos")
def list_videos(limit: int = 25):
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, key, created_at FROM videos ORDER BY created_at DESC LIMIT :lim"
        ), {"lim": limit}).fetchall()
    return [{"id": r[0], "key": r[1], "created_at": str(r[2])} for r in rows]

# --- Videos: summary (renditions + status) ---
@app.get("/videos/{video_id}/summary")
def video_summary(video_id: str):
    with engine.begin() as conn:
        v = conn.execute(text(
            "SELECT id, key, created_at FROM videos WHERE id=:id"
        ), {"id": video_id}).first()
        if not v:
            raise HTTPException(status_code=404, detail="video not found")
        rend = conn.execute(text(
            "SELECT height, status, key FROM renditions WHERE video_id=:id ORDER BY height"
        ), {"id": video_id}).fetchall()
    return {
        "id": v[0],
        "source_key": v[1],
        "created_at": str(v[2]),
        "renditions": [{"height": r[0], "status": r[1], "key": r[2]} for r in rend],
    }


# NextTube

> Local-first, end-to-end **video upload → transcode → HLS playback** pipeline.  
> Tech stack: **Next.js (App Router) + FastAPI + Worker (FFmpeg) + Postgres + Redis + MinIO** — all wired with Docker Compose.

---

## ✨ What it does

- **Upload** a video from the browser
- **Queue** a transcode job (240p / 480p / 720p by default)
- **Transcode** to HLS via a Python worker using **FFmpeg**
- **Store** source & HLS assets in **MinIO** (S3-compatible)
- **Stream** via the API (proxy to MinIO) to avoid CORS headaches
- **Watch** with an HLS player (`hls.js`) at `/watch/<video_id>`
- **List & inspect** recent videos and their renditions

---

## 🧭 Architecture (high level)

1. Browser looking for master playlist

   ```text
   Browser (Next.js)
   ├── POST /upload ─────────────► API (FastAPI) ──► MinIO : source/<id>.mp4
   ├── POST /jobs/transcode ─────► API ────────────► Redis : jobs:transcode
   └── GET /videos/<id>/playlist ► API (proxy) ───► MinIO : HLS/<id>/...

2. Worker creates master playlist

Worker (Python + FFmpeg)
├── BRPOP jobs:transcode (Redis)
├── Download source from MinIO
├── FFmpeg → HLS variants (240/480/720)
├── Upload HLS to MinIO (HLS/<id>/...)
└── Update Postgres: jobs & renditions statuses

```text


## 📁 Repo layout
```
infra/
docker-compose.yml # all services: web, api, worker, postgres, redis, minio, createbuckets
apps/
api/
app/
main.py # FastAPI routes (upload, jobs, HLS proxy, list/summary)
db.py # SQLAlchemy engine
s3.py # MinIO/S3 helpers
requirements.txt
Dockerfile
worker/
main.py # Redis consumer + FFmpeg + MinIO + Postgres updates
requirements.txt
Dockerfile
web/
app/
page.tsx # Home (upload + recent list)
watch/[id]/page.tsx # HLS player (hls.js)
package.json
next.config.js
tsconfig.json
.env.example # example env vars (copy to .env)
.gitignore # keep secrets, node_modules, builds out of git

```

## 🚀 Quick start

> Prereq: **Docker Desktop** running.

```bash
# from project root
cp .env.example .env

# bring everything up
docker compose -f infra/docker-compose.yml up --build -d

```
Open:

- Web: http://localhost:3000

- API docs (FastAPI): http://localhost:8000/docs

- Health: http://localhost:8000/healthz

- MinIO Console: http://localhost:9001 (user: minioadmin, pass: minioadmin)

To follow logs:

```
docker logs -f media_web
docker logs -f media_api
docker logs -f media_worker

```


## 🖥️ Using the app (happy path)
1. Go to http://localhost:3000/

2. Choose a video file → click Upload & Transcode

3. You’ll be redirected to /watch/<video_id>

4. The player will pull the master playlist from the API and play when renditions are ready

You can also hit the endpoints directly:

```
# Upload
curl -F "file=@myvideo.mp4" http://localhost:8000/upload
# → { "video_id": "...", "key": "source/<id>.mp4" }

# Queue a job
curl -H "Content-Type: application/json" \
  -d '{"video_id":"<id>","profiles":[240,480,720]}' \
  http://localhost:8000/jobs/transcode

# Check job
curl http://localhost:8000/jobs/<job_id>

# Master playlist (API proxy → MinIO)
curl http://localhost:8000/videos/<video_id>/playlist

```


## 🧩 API surface (current)
Method | Path| Purpose
--- | --- | --- |
GET	| /healthz | Liveness (db/redis)
POST | /upload | Upload source to MinIO, insert videos
POST | /jobs/transcode | Create job, enqueue in Redis
GET | /jobs/{job_id} | Job status
GET | /videos | List recent uploads
GET | /videos/{video_id}/summary | Source + renditions & statuses
GET | /videos/{video_id}/playlist | Master HLS playlist (proxy)
GET | /videos/{video_id}/{path} | Variant playlists / TS segments (proxy)

> The /videos/... routes proxy MinIO so the browser never talks to MinIO directly (CORS-friendly).

## 🧠 How it works (deeper)
- # Upload

    - POST /upload saves bytes to media/source/<video_id>.mp4 via put_bytes() in app/s3.py

    - inserts videos(id, key) in Postgres

- # Transcode job

    - POST /jobs/transcode inserts jobs row (queued) and renditions rows (queued)

    - pushes JSON to Redis list jobs:transcode

- # Worker

    - BRPOP jobs:transcode → marks job running, renditions running

    - downloads source/<id>.mp4 from MinIO

    - runs FFmpeg per profile (scale=-2:<height>, H.264 + AAC, HLS VOD)

    - writes index.m3u8 master playlist

    - uploads to HLS/<id>/... in MinIO with correct Content-Type

    - sets renditions ready, job done

- # Playback

    - /watch/<id> uses hls.js to fetch /videos/<id>/playlist

    - API streams MinIO objects to the browser (no CORS setup required on MinIO)

## ⚙️ Environment
See .env.example (copy to .env):
```
# Database
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=media
DATABASE_URL=postgresql://postgres:postgres@postgres:5432/media

# Redis
REDIS_URL=redis://redis:6379/0

# MinIO / S3
S3_ENDPOINT=http://minio:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
S3_BUCKET=media

```
## 🧪 Verifications & useful commands

```

# DB checks (inside the postgres container)
docker exec -it media_postgres psql -U postgres -d media -c "select id,key,created_at from videos order by created_at desc limit 5"
docker exec -it media_postgres psql -U postgres -d media -c "select id,video_id,status,type from jobs order by created_at desc limit 5"
docker exec -it media_postgres psql -U postgres -d media -c "select video_id,height,status,key from renditions order by video_id,height"

```
## 🛠️ Troubleshooting

- # 405 Method Not Allowed on playlists

    - curl -I sends HEAD; use GET (browsers use GET).

- # Empty reply from server on /videos/...

    - Check docker logs -f media_api — usually an exception (e.g., wrong import or missing env).

- # Worker crash: invalid dsn ... psycopg2

    - Worker must use postgresql://... (not postgresql+psycopg2://)

- # 404 on /watch/<id>

    - Ensure path is exactly apps/web/app/watch/[id]/page.tsx

    - Restart web: docker restart media_web

- # CORS errors

    - You shouldn’t see them — the API proxies MinIO. Make sure the web player hits /videos/... on the API, not MinIO directly.

## 🗺️ Roadmap (nice-to-haves)

- Player QoE metrics (startup time, rebuffer count) → store + small dashboard

- Auth / signed URLs for protected content

- Retries / DLQ for failed jobs + basic alerting

- Thumbnails & preview sprites

- Configurable ladder + presets (quality vs speed)

- Optional: swap MinIO for AWS S3 (no code changes beyond env)

## 📜 License
MIT (or your choice)

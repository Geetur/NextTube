import os, json, uuid, shutil, tempfile, subprocess, pathlib, time
import redis, boto3, psycopg2

REDIS_URL    = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/media")

# Allow SQLAlchemy-style url too
if DATABASE_URL.startswith("postgresql+psycopg2://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL.split("postgresql+psycopg2://", 1)[1]

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_REGION   = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS   = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET   = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET   = os.getenv("S3_BUCKET", "media")

# Heights → (video_kbps, audio_kbps)
PROFILE_PRESETS = {
    240: (400, 96),
    480: (800, 96),
    720: (1500, 128),
}

def s3():
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=S3_ACCESS,
        aws_secret_access_key=S3_SECRET,
        endpoint_url=S3_ENDPOINT,
    )

def pg():
    return psycopg2.connect(DATABASE_URL)

def run(cmd: list[str]) -> int:
    print("[ffmpeg]>", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(proc.stdout.decode(errors="ignore")[-4000:])  # last chunk of logs
    return proc.returncode

def upload_dir(local_dir: str, s3_prefix: str):
    client = s3()
    for p in pathlib.Path(local_dir).glob("*"):
        key = f"{s3_prefix}/{p.name}".replace("\\", "/")
        if p.suffix.lower() == ".m3u8":
            ct = "application/vnd.apple.mpegurl"
        elif p.suffix.lower() == ".ts":
            ct = "video/MP2T"
        else:
            ct = "application/octet-stream"
        client.upload_file(str(p), S3_BUCKET, key, ExtraArgs={"ContentType": ct})
        print("[upload]", key, ct)

def build_master(profiles: list[int]) -> str:
    # BANDWIDTH in bits/sec (video + audio)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for h in profiles:
        vkbps, akbps = PROFILE_PRESETS.get(h, (800, 96))
        bw = (vkbps + akbps) * 1000
        # Some players are okay without RESOLUTION; keep it minimal.
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bw}")
        lines.append(f"{h}.m3u8")
    return "\n".join(lines) + "\n"

def transcode_variants(src_path: str, out_dir: str, profiles: list[int]) -> dict[int, bool]:
    """Run one ffmpeg per profile → <height>.m3u8 + <height>_###.ts"""
    ok = {}
    for h in profiles:
        vkbps, akbps = PROFILE_PRESETS.get(h, (800, 96))
        # Conservative rate control
        maxrate = int(vkbps * 1.1)
        bufsize = int(vkbps * 2.0)
        out_m3u8 = os.path.join(out_dir, f"{h}.m3u8")
        seg_tmpl = os.path.join(out_dir, f"{h}_%03d.ts")
        cmd = [
            "ffmpeg", "-y",
            "-i", src_path,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", f"scale=-2:{h}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{vkbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            "-c:a", "aac", "-b:a", f"{akbps}k",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_playlist_type", "vod",
            "-hls_list_size", "0",
            "-hls_segment_filename", seg_tmpl,
            out_m3u8,
        ]
        rc = run(cmd)
        ok[h] = (rc == 0 and os.path.exists(out_m3u8))
    return ok

def process_job(job: dict):
    job_id   = job["job_id"]
    video_id = job["video_id"]
    profiles = [int(h) for h in job.get("profiles", [240,480,720])]
    print(f"[worker] processing job={job_id} video={video_id} profiles={profiles}")

    # Fetch source key from DB
    with pg() as conn, conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='running', updated_at=NOW() WHERE id=%s", (job_id,))
        cur.execute("SELECT key FROM videos WHERE id=%s", (video_id,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError("video not found in DB")
        source_key = row[0]

    work = tempfile.mkdtemp(prefix=f"hls_{video_id}_")
    src_path = os.path.join(work, "source.mp4")
    out_dir  = os.path.join(work, "out")
    os.makedirs(out_dir, exist_ok=True)

    # Download source from S3 → /tmp
    print("[download] s3://%s/%s -> %s" % (S3_BUCKET, source_key, src_path))
    s3().download_file(S3_BUCKET, source_key, src_path)

    # Create/ensure rendition rows exist and mark 'running'
    with pg() as conn, conn.cursor() as cur:
        for h in profiles:
            cur.execute(
                "INSERT INTO renditions(id, video_id, height, status) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (str(uuid.uuid4()), video_id, h, "queued")
            )
            cur.execute(
                "UPDATE renditions SET status='running' WHERE video_id=%s AND height=%s",
                (video_id, h)
            )

    try:
        results = transcode_variants(src_path, out_dir, profiles)

        # Build master playlist
        master = build_master(profiles)
        (pathlib.Path(out_dir) / "index.m3u8").write_text(master, encoding="utf-8")

        # Upload everything to s3://media/HLS/{video_id}/...
        prefix = f"HLS/{video_id}"
        upload_dir(out_dir, prefix)

        # Update DB per-rendition
        with pg() as conn, conn.cursor() as cur:
            for h, ok in results.items():
                status = "ready" if ok else "failed"
                key = f"{prefix}/{h}.m3u8"
                cur.execute(
                    "UPDATE renditions SET status=%s, key=%s WHERE video_id=%s AND height=%s",
                    (status, key, video_id, h)
                )
            cur.execute("UPDATE jobs SET status='done', updated_at=NOW() WHERE id=%s", (job_id,))
        print(f"[worker] job {job_id} done.")
    except Exception as e:
        print("[worker] ERROR:", repr(e))
        with pg() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET status='failed', updated_at=NOW() WHERE id=%s", (job_id,))
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass

def main():
    r = redis.Redis.from_url(REDIS_URL)
    print("[worker] redis ping:", r.ping())
    while True:
        item = r.brpop("jobs:transcode", timeout=5)
        if not item:
            continue
        _, payload = item
        job = json.loads(payload.decode())
        process_job(job)
        time.sleep(0.05)

if __name__ == "__main__":
    main()



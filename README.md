# Media Optimizer — Day 1 Skeleton

## Run
```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
```

## Verify
- API: http://localhost:8000/healthz (returns {"ok": true, "db": true/false})
- Web: http://localhost:3000
- MinIO Console: http://localhost:9001 (user: minioadmin / pass: minioadmin)
- Alembic: inside the api container, `alembic current`

## Next (Day 2 preview)
- Add `/upload` endpoint (store source video -> MinIO)
- Add `jobs` table + `POST /jobs/transcode`

# Audio Separator

FastAPI service that separates audio from any yt-dlp compatible URL into stem tracks.

## Setup

```bash
# Install dependencies
uv sync

# Set required environment variables
export CLOUDFLARE_ACCOUNT_ID=your_account_id
export R2_ACCESS_KEY_ID=your_access_key
export R2_SECRET_ACCESS_KEY=your_secret_key
export R2_PUBLIC_DOMAIN=your_domain
export API_SECRET_KEY=your_api_secret_key
export JOB_ACCESS_TOKEN_SECRET=your_job_access_token_secret
export REDIS_URL=rediss://username:password@your-upstash-redis.com:6379

# Start services
docker-compose up --scale worker=2
# OR manually: uv run python main.py && uv run python worker.py
```

## Usage

```bash
# Start job
curl -X POST http://localhost:5500/separate-audio \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_api_secret_key" \
  -d '{"audio_url": "https://soundcloud.com/artist/track", "request_id": "your-correlation-id"}'

# Check status
curl "http://localhost:5500/job/your-track-id?access_token=your_job_access_token"

# Stream progress
curl -N "http://localhost:5500/job/your-track-id/stream?access_token=your_job_access_token"
```

## API

| Endpoint | Description |
|----------|-------------|
| `POST /separate-audio` | Start audio separation job |
| `GET /job/{track_id}` | Get job status and results with a per-job access token |
| `GET /job/{track_id}/stream` | Stream job progress with a per-job access token |
| `GET /health` | Health check |

### Request

```bash
curl -X POST http://localhost:5500/separate-audio \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_api_secret_key" \
  -d '{"audio_url": "https://soundcloud.com/artist/track", "request_id": "your-correlation-id"}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `audio_url` | string | Yes | Any yt-dlp compatible URL (YouTube, SoundCloud, Vimeo, etc.) |
| `request_id` | string | Yes | Caller-provided correlation ID for webhook callbacks |

### Response

```json
{
  "track_id": "uuid",
  "request_id": "your-correlation-id",
  "status": "processing",
  "message": "Audio separation started",
  "access_token": "signed-job-token",
  "access_token_expires_at": 1234569999
}
```

The intended flow is:
- Cloudflare Worker calls `POST /separate-audio` with the server-side API key after auth and credit checks.
- FastAPI returns a short-lived `access_token` scoped to the `track_id`.
- React uses that token directly against `GET /job/{track_id}` and `GET /job/{track_id}/stream`.

## Configuration

Required environment variables:

| Variable | Description |
|----------|-------------|
| `API_SECRET_KEY` | API authentication key |
| `JOB_ACCESS_TOKEN_SECRET` | Secret used to sign browser-facing per-job access tokens; falls back to `API_SECRET_KEY` if unset |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 storage access key |
| `R2_SECRET_ACCESS_KEY` | R2 storage secret key |
| `R2_PUBLIC_DOMAIN` | R2 bucket domain |
| `REDIS_URL` | Redis connection URL; defaults to `redis://redis:6379` in Docker |

Optional:

- `JOB_ACCESS_TOKEN_TTL_SECONDS` - Per-job token lifetime in seconds (default: `3600`)
- `PORT` - Server port (default: 5500)

## Deployment

### Docker

```bash
docker-compose up --scale worker=2
```

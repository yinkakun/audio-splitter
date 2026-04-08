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
curl -H "Authorization: Bearer your_api_secret_key" \
  http://localhost:5500/job/your-track-id
```

## API

| Endpoint | Description |
|----------|-------------|
| `POST /separate-audio` | Start audio separation job |
| `GET /job/{track_id}` | Get job status and results |
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
  "message": "Audio separation started"
}
```

### Webhook Payload

When a job completes (or fails), a webhook is sent to the configured `WEBHOOK_URL`. Multiple requests for the same URL are deduplicated - only one job runs, and all `request_id`s are included in the callback.

**Success:**

```json
{
  "status": "completed",
  "track_id": "uuid",
  "audio_url": "https://soundcloud.com/artist/track",
  "request_ids": ["request-1", "request-2"],
  "progress": 100,
  "created_at": 1234567890.0,
  "result": {
    "stems_urls": {
      "vocals": "https://...",
      "drums": "https://...",
      "bass": "https://...",
      "other": "https://..."
    }
  }
}
```

**Failure:**

```json
{
  "status": "failed",
  "track_id": "uuid",
  "audio_url": "https://soundcloud.com/artist/track",
  "request_ids": ["request-1", "request-2"],
  "progress": 0,
  "created_at": 1234567890.0,
  "error": "Error message"
}
```

## Configuration

Required environment variables:

| Variable | Description |
|----------|-------------|
| `API_SECRET_KEY` | API authentication key |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 storage access key |
| `R2_SECRET_ACCESS_KEY` | R2 storage secret key |
| `R2_PUBLIC_DOMAIN` | R2 bucket domain |
| `REDIS_URL` | Redis connection URL; defaults to `redis://redis:6379` in Docker |

Optional:

- `WEBHOOK_URL` - Job completion notifications
- `PORT` - Server port (default: 5500)

## Deployment

### Docker

```bash
docker-compose up --scale worker=2
```

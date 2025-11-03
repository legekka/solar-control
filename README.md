# Solar Control

A coordinator for multiple solar-host instances with OpenAI-compatible API gateway.

## Features

- Manage multiple solar-host instances
- OpenAI-compatible API gateway with model routing
- Model alias resolution (exact match; optional prefix fallback)
- Host-aware, model-size-weighted load balancing (prefers free hosts; otherwise chooses lowest active parameter load; round-robin tiebreaker)
- Transparent authentication handling
- WebSocket log aggregation
- Docker support

## Installation

```bash
# Install dependencies
pip install -r requirements.txt
```

## Configuration

Create a `.env` file or set environment variables:

```bash
API_KEY=your-gateway-api-key-here
HOST=0.0.0.0
PORT=8000
```

## Running Natively

```bash
# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Running with Docker

```bash
# Create the data directory first (if it doesn't exist)
mkdir -p data

# Build and start with docker-compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

**Note:** Configuration and hosts are stored in the `data/` directory, which is mounted as a volume. This ensures your data persists across container restarts.

## API Endpoints

### Host Management

- `POST /hosts` - Register a new solar-host
- `GET /hosts` - List all solar-hosts
- `DELETE /hosts/{host_id}` - Remove a solar-host
- `GET /hosts/{host_id}/instances` - Get instances from a host

### OpenAI Gateway

- `POST /v1/chat/completions` - Chat completions (routed by model)
- `POST /v1/completions` - Text completions (routed by model)
- `GET /v1/models` - List all available models

### Proxy Endpoints

- `POST /hosts/{host_id}/instances/{instance_id}/start` - Start instance
- `POST /hosts/{host_id}/instances/{instance_id}/stop` - Stop instance
- `POST /hosts/{host_id}/instances/{instance_id}/restart` - Restart instance

## Authentication

All requests require an `X-API-Key` header with your configured gateway API key.

Solar-control handles authentication to solar-hosts transparently using stored credentials.

## Example Host Registration

```json
{
  "name": "Mac Studio 1",
  "url": "http://192.168.1.100:8001",
  "api_key": "host-specific-api-key"
}
```

## OpenAI Gateway Usage

Once hosts are registered and instances are running, use the gateway like any OpenAI API:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "X-API-Key: your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss:120b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

The gateway automatically routes to the correct host based on the model alias.


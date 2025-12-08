# Solar Control

A coordinator for multiple solar-host instances with OpenAI-compatible API gateway. Supports multiple backend types including llama.cpp and HuggingFace models.

## Features

- **Multi-backend support** - Route to llama.cpp, HuggingFace Causal LM, Classification, and Embedding models
- Manage multiple solar-host instances
- OpenAI-compatible API gateway with model routing
- **Classification endpoint** - Custom `/v1/classify` endpoint for sequence classification models
- Model alias resolution (exact match; optional prefix fallback)
- Host-aware, model-size-weighted load balancing (prefers free hosts; otherwise chooses lowest active parameter load; round-robin tiebreaker)
- **Endpoint-aware routing** - Routes requests only to instances that support the requested endpoint
- Transparent authentication handling
- WebSocket log aggregation
- Docker support

## Supported Backend Types

| Backend | Endpoints |
|---------|-----------|
| **llama.cpp** | `/v1/chat/completions`, `/v1/completions`, `/v1/models` |
| **HuggingFace Causal** | `/v1/chat/completions`, `/v1/completions`, `/v1/models` |
| **HuggingFace Classification** | `/v1/classify`, `/v1/models` |
| **HuggingFace Embedding** | `/v1/embeddings`, `/v1/models` |

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

### Classification Gateway

- `POST /v1/classify` - Text classification (routed by model to HuggingFace Classification instances)

### Embeddings Gateway

- `POST /v1/embeddings` - Text embeddings (routed by model to HuggingFace Embedding instances)

### Proxy Endpoints

- `POST /hosts/{host_id}/instances/{instance_id}/start` - Start instance
- `POST /hosts/{host_id}/instances/{instance_id}/stop` - Stop instance
- `POST /hosts/{host_id}/instances/{instance_id}/restart` - Restart instance

## Authentication

All requests require an `X-API-Key` header (or `Authorization: Bearer <key>`) with your configured gateway API key.

Solar-control handles authentication to solar-hosts transparently using stored credentials.

## Example Host Registration

```json
{
  "name": "GPU Server 1",
  "url": "http://192.168.1.100:8001",
  "api_key": "host-specific-api-key"
}
```

## Gateway Usage Examples

### Chat Completions (llama.cpp or HuggingFace Causal)

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "X-API-Key: your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3:8b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Text Classification (HuggingFace Classification)

```bash
curl http://localhost:8000/v1/classify \
  -H "X-API-Key: your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "classifier:deberta",
    "input": "This product is amazing! I love it."
  }'
```

**Classification Response:**

```json
{
  "id": "clf-abc123",
  "object": "classification",
  "model": "classifier:deberta",
  "choices": [
    {
      "index": 0,
      "label": "positive",
      "score": 0.9876
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "total_tokens": 12
  }
}
```

### Batch Classification

```bash
curl http://localhost:8000/v1/classify \
  -H "X-API-Key: your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "classifier:deberta",
    "input": [
      "This is great!",
      "This is terrible."
    ]
  }'
```

### Text Embeddings (HuggingFace Embedding)

```bash
curl http://localhost:8000/v1/embeddings \
  -H "X-API-Key: your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "embed:minilm",
    "input": "Hello, world!"
  }'
```

**Embedding Response:**

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "embedding": [0.0123, -0.0456, 0.0789, ...],
      "index": 0
    }
  ],
  "model": "embed:minilm",
  "usage": {
    "prompt_tokens": 4,
    "total_tokens": 4
  }
}
```

### Batch Embeddings

```bash
curl http://localhost:8000/v1/embeddings \
  -H "X-API-Key: your-gateway-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "embed:minilm",
    "input": [
      "First text to embed",
      "Second text to embed"
    ]
  }'
```

## Routing Behavior

Solar-control automatically routes requests based on:

1. **Model alias** - Matches the `model` field to instance aliases
2. **Endpoint support** - Only routes to instances that support the requested endpoint
3. **Load balancing** - Prefers idle instances; distributes load based on model size

For example:
- A `/v1/chat/completions` request will only route to llama.cpp or HuggingFace Causal instances
- A `/v1/classify` request will only route to HuggingFace Classification instances
- A `/v1/embeddings` request will only route to HuggingFace Embedding instances

The gateway automatically discovers which endpoints each instance supports when the model registry is refreshed.

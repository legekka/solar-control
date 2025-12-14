from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.models import (
    ChatCompletionRequest,
    CompletionRequest,
    ClassifyRequest,
    EmbeddingRequest,
    RerankRequest,
)
from app.gateway import gateway


router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models")
async def list_models():
    """List all available models (OpenAI compatible)"""
    try:
        models = await gateway.get_available_models()
        return {"object": "list", "data": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, client: Request):
    """Chat completions endpoint (OpenAI compatible)"""
    try:
        # Get client IP
        client_ip = client.client.host if client.client else "unknown"

        # Convert request to dict
        request_data = request.model_dump(exclude_none=True)

        # Check if streaming
        if request.stream:
            # Stream response
            async def stream_generator():
                try:
                    async for chunk in gateway.stream_request(
                        request.model, "/v1/chat/completions", request_data, client_ip
                    ):
                        yield chunk
                except Exception as e:
                    yield f'data: {{"error": "{str(e)}"}}\n\n'.encode()

            return StreamingResponse(stream_generator(), media_type="text/event-stream")
        else:
            # Non-streaming response
            response = await gateway.route_request(
                request.model, "/v1/chat/completions", request_data, client_ip
            )
            return response

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/completions")
async def completions(request: CompletionRequest, client: Request):
    """Text completions endpoint (OpenAI compatible)"""
    try:
        # Get client IP
        client_ip = client.client.host if client.client else "unknown"

        # Convert request to dict
        request_data = request.model_dump(exclude_none=True)

        # Check if streaming
        if request.stream:
            # Stream response
            async def stream_generator():
                try:
                    async for chunk in gateway.stream_request(
                        request.model, "/v1/completions", request_data, client_ip
                    ):
                        yield chunk
                except Exception as e:
                    yield f'data: {{"error": "{str(e)}"}}\n\n'.encode()

            return StreamingResponse(stream_generator(), media_type="text/event-stream")
        else:
            # Non-streaming response
            response = await gateway.route_request(
                request.model, "/v1/completions", request_data, client_ip
            )
            return response

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/classify")
async def classify(request: ClassifyRequest, client: Request):
    """Classification endpoint for HuggingFace SequenceClassification models.

    Routes to instances that support the /v1/classify endpoint.
    """
    try:
        # Get client IP
        client_ip = client.client.host if client.client else "unknown"

        # Convert request to dict
        request_data = request.model_dump(exclude_none=True)

        # Route to classification-capable instance
        response = await gateway.route_request(
            request.model,
            "/v1/classify",
            request_data,
            client_ip,
            required_endpoint="/v1/classify",
        )
        return response

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/embeddings")
async def embeddings(request: EmbeddingRequest, client: Request):
    """Embeddings endpoint (OpenAI compatible).

    Routes to instances that support the /v1/embeddings endpoint.
    Returns embedding vectors from HuggingFace models using mean pooling
    of the last hidden state.
    """
    try:
        # Get client IP
        client_ip = client.client.host if client.client else "unknown"

        # Convert request to dict
        request_data = request.model_dump(exclude_none=True)

        # Route to embedding-capable instance
        response = await gateway.route_request(
            request.model,
            "/v1/embeddings",
            request_data,
            client_ip,
            required_endpoint="/v1/embeddings",
        )
        return response

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rerank")
async def rerank(request: RerankRequest, client: Request):
    """Rerank endpoint (OpenAI compatible).

    Routes to instances that support the /v1/rerank endpoint.
    Reranks documents based on relevance to a query using reranker models.
    """
    try:
        # Get client IP
        client_ip = client.client.host if client.client else "unknown"

        # Convert request to dict
        request_data = request.model_dump(exclude_none=True)

        # Route to reranker-capable instance
        response = await gateway.route_request(
            request.model,
            "/v1/rerank",
            request_data,
            client_ip,
            required_endpoint="/v1/rerank",
        )
        return response

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

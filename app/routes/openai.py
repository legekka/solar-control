from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.models import ChatCompletionRequest, CompletionRequest
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
                        request.model,
                        "/v1/chat/completions",
                        request_data,
                        client_ip
                    ):
                        yield chunk
                except Exception as e:
                    yield f"data: {{\"error\": \"{str(e)}\"}}\n\n".encode()
            
            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream"
            )
        else:
            # Non-streaming response
            response = await gateway.route_request(
                request.model,
                "/v1/chat/completions",
                request_data,
                client_ip
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
                        request.model,
                        "/v1/completions",
                        request_data,
                        client_ip
                    ):
                        yield chunk
                except Exception as e:
                    yield f"data: {{\"error\": \"{str(e)}\"}}\n\n".encode()
            
            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream"
            )
        else:
            # Non-streaming response
            response = await gateway.route_request(
                request.model,
                "/v1/completions",
                request_data,
                client_ip
            )
            return response
            
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


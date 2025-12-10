from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio

from app.config import settings
from app.gateway import gateway
from app.routes import hosts, openai, websockets
from app.routes import gateway as gateway_routes
from app.routes.websockets import broadcast_host_status


async def refresh_hosts_periodically():
    """Background task to check hosts NOT connected via WebSocket.

    WebSocket 2.0: Hosts connected via WebSocket push their status/health directly.
    This task only polls hosts that haven't established a WebSocket connection yet
    (e.g., hosts that haven't been updated to 2.0).
    """
    import aiohttp
    from app.config import host_manager
    from app.models import HostStatus
    from app.routes.websockets import host_connection_manager

    while True:
        try:
            await asyncio.sleep(
                30
            )  # Check less frequently (30s) - WebSocket handles most updates

            hosts = host_manager.get_all_hosts()
            if not hosts:
                continue

            async with aiohttp.ClientSession() as session:
                for host in hosts:
                    # Skip hosts that are connected via WebSocket
                    if host_connection_manager.is_host_connected(host.id):
                        continue

                    old_status = host.status
                    memory_data = None

                    try:
                        # Quick health check
                        url = f"{host.url}/health"
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=3)
                        ) as response:
                            if response.status == 200:
                                # Verify API key works
                                url = f"{host.url}/instances"
                                headers = {"X-API-Key": host.api_key}
                                async with session.get(
                                    url,
                                    headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=3),
                                ) as inst_response:
                                    if inst_response.status == 200:
                                        host_manager.update_host_status(
                                            host.id, HostStatus.ONLINE
                                        )
                                        new_status = "online"

                                        # Fetch memory information
                                        try:
                                            memory_url = f"{host.url}/memory"
                                            async with session.get(
                                                memory_url,
                                                headers=headers,
                                                timeout=aiohttp.ClientTimeout(total=3),
                                            ) as mem_response:
                                                if mem_response.status == 200:
                                                    memory_data = (
                                                        await mem_response.json()
                                                    )
                                                    # Update host with memory data
                                                    from app.models import MemoryInfo

                                                    host.memory = MemoryInfo(
                                                        **memory_data
                                                    )
                                                    host_manager.save()
                                        except Exception:
                                            # Memory fetch failed, but host is still online
                                            pass
                                    else:
                                        host_manager.update_host_status(
                                            host.id, HostStatus.ERROR
                                        )
                                        new_status = "error"
                            else:
                                host_manager.update_host_status(
                                    host.id, HostStatus.ERROR
                                )
                                new_status = "error"
                    except Exception:
                        host_manager.update_host_status(host.id, HostStatus.OFFLINE)
                        new_status = "offline"

                    # Broadcast if status changed or memory updated
                    if old_status.value != new_status or memory_data:
                        await broadcast_host_status(
                            {
                                "host_id": host.id,
                                "name": host.name,
                                "status": new_status,
                                "url": host.url,
                                "memory": memory_data,
                            }
                        )
        except Exception as e:
            print(f"Error in host refresh task: {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the application"""
    from app.gateway_logs import gateway_logger
    from app.config import host_manager
    
    # Startup
    print("Starting Solar Control...")
    print(f"Gateway API Key configured: {settings.api_key[:4]}...")

    # Start gateway logger background flush task
    await gateway_logger.start()
    print("Gateway logger started")

    # Start gateway background tasks (registry refresh + health probes)
    await gateway.start_background_tasks()
    print("Gateway background tasks started")

    # Start background task for host status monitoring
    task = asyncio.create_task(refresh_hosts_periodically())
    print("Host status monitoring started")
    print("Solar Control started successfully")

    yield

    # Shutdown
    print("Shutting down Solar Control...")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Stop gateway background tasks and close session
    try:
        await gateway.stop_background_tasks()
    finally:
        await gateway.close()
    
    # Flush host manager (save any pending changes)
    try:
        await host_manager.flush_save()
    except Exception as e:
        print(f"Error flushing host manager: {e}")
    
    # Stop gateway logger (flushes remaining buffer)
    try:
        await gateway_logger.stop()
    except Exception as e:
        print(f"Error stopping gateway logger: {e}")
    
    print("Solar Control shut down")


app = FastAPI(
    title="Solar Control",
    description="Coordinator for multiple solar-host instances with OpenAI-compatible API gateway. Supports llama.cpp, HuggingFace CausalLM, and HuggingFace Classification backends.",
    version="2.0.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Key authentication middleware
@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """Verify API key for all requests except health check and OpenAPI docs"""
    # Allow CORS preflight requests (OPTIONS) without authentication
    if request.method == "OPTIONS":
        return await call_next(request)

    # Allow access to health check, docs, OpenAPI schema, and WebSockets
    public_paths = ["/health", "/", "/docs", "/redoc", "/openapi.json"]
    if request.url.path in public_paths or request.url.path.startswith("/ws/"):
        return await call_next(request)

    # Support both X-API-Key and Authorization: Bearer for OpenAI compatibility
    api_key = None

    # Check X-API-Key header (for internal/host management)
    x_api_key = request.headers.get("X-API-Key")
    if x_api_key:
        api_key = x_api_key

    # Check Authorization: Bearer header (for OpenAI compatibility)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[7:]  # Remove "Bearer " prefix

    if not api_key or api_key != settings.api_key:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": {
                    "message": "Incorrect API key provided. You can find your API key in your configuration.",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key",
                }
            },
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
            },
        )

    return await call_next(request)


# Include routers
app.include_router(hosts.router)
app.include_router(openai.router)
app.include_router(websockets.router, prefix="/ws")
app.include_router(gateway_routes.router)


# Customize OpenAPI schema to add security
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # Add security schemes (support both X-API-Key and Bearer token)
    openapi_schema["components"]["securitySchemes"] = {
        "APIKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "API Key for host management endpoints",
        },
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "Bearer token for OpenAI-compatible endpoints",
        },
    }

    # Apply security to all paths except public ones
    public_paths = ["/health", "/", "/docs", "/redoc", "/openapi.json"]
    for path, path_item in openapi_schema["paths"].items():
        if path not in public_paths:
            for operation in path_item.values():
                if isinstance(operation, dict):
                    # OpenAI endpoints support Bearer token, others use X-API-Key
                    if path.startswith("/v1/"):
                        operation["security"] = [
                            {"BearerAuth": []},
                            {"APIKeyHeader": []},
                        ]
                    else:
                        operation["security"] = [{"APIKeyHeader": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "solar-control", "version": "2.0.0"}


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "solar-control",
        "version": "2.0.0",
        "description": "Coordinator for multiple solar-host instances with OpenAI-compatible API gateway",
        "supported_backends": [
            "llamacpp",
            "huggingface_causal",
            "huggingface_classification",
        ],
        "endpoints": [
            "/v1/models",
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/classify",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)

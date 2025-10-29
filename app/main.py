from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio

from app.config import settings
from app.gateway import gateway
from app.routes import hosts, openai, websockets
from app.routes.websockets import broadcast_host_status


async def refresh_hosts_periodically():
    """Background task to refresh host statuses every 10 seconds"""
    import aiohttp
    from app.config import host_manager
    from app.models import HostStatus
    
    while True:
        try:
            await asyncio.sleep(10)  # Check every 10 seconds
            
            hosts = host_manager.get_all_hosts()
            if not hosts:
                continue
            
            async with aiohttp.ClientSession() as session:
                for host in hosts:
                    old_status = host.status
                    try:
                        # Quick health check
                        url = f"{host.url}/health"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as response:
                            if response.status == 200:
                                # Verify API key works
                                url = f"{host.url}/instances"
                                headers = {"X-API-Key": host.api_key}
                                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as inst_response:
                                    if inst_response.status == 200:
                                        host_manager.update_host_status(host.id, HostStatus.ONLINE)
                                        new_status = "online"
                                    else:
                                        host_manager.update_host_status(host.id, HostStatus.ERROR)
                                        new_status = "error"
                            else:
                                host_manager.update_host_status(host.id, HostStatus.ERROR)
                                new_status = "error"
                    except Exception:
                        host_manager.update_host_status(host.id, HostStatus.OFFLINE)
                        new_status = "offline"
                    
                    # Broadcast status change if different
                    if old_status.value != new_status:
                        await broadcast_host_status({
                            "host_id": host.id,
                            "name": host.name,
                            "status": new_status,
                            "url": host.url
                        })
        except Exception as e:
            print(f"Error in host refresh task: {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the application"""
    # Startup
    print("Starting Solar Control...")
    print(f"Gateway API Key configured: {settings.api_key[:4]}...")
    
    # Refresh model registry
    await gateway.refresh_model_registry()
    print("Model registry initialized")
    
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
    await gateway.close()
    print("Solar Control shut down")


app = FastAPI(
    title="Solar Control",
    description="Coordinator for multiple solar-host instances with OpenAI-compatible API gateway",
    version="1.0.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True}
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
    
    # Allow access to health check, docs, and OpenAPI schema
    public_paths = ["/health", "/", "/docs", "/redoc", "/openapi.json"]
    if request.url.path in public_paths:
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
                    "code": "invalid_api_key"
                }
            },
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
            }
        )
    
    return await call_next(request)


# Include routers
app.include_router(hosts.router)
app.include_router(openai.router)
app.include_router(websockets.router)


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
            "description": "API Key for host management endpoints"
        },
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "Bearer token for OpenAI-compatible endpoints"
        }
    }
    
    # Apply security to all paths except public ones
    public_paths = ["/health", "/", "/docs", "/redoc", "/openapi.json"]
    for path, path_item in openapi_schema["paths"].items():
        if path not in public_paths:
            for operation in path_item.values():
                if isinstance(operation, dict):
                    # OpenAI endpoints support Bearer token, others use X-API-Key
                    if path.startswith("/v1/"):
                        operation["security"] = [{"BearerAuth": []}, {"APIKeyHeader": []}]
                    else:
                        operation["security"] = [{"APIKeyHeader": []}]
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "solar-control",
        "version": "1.0.0"
    }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "solar-control",
        "version": "1.0.0",
        "description": "Coordinator for multiple solar-host instances with OpenAI-compatible API gateway"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )


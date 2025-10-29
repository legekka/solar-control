from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from app.config import settings
from app.gateway import gateway
from app.routes import hosts, openai, websockets


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the application"""
    # Startup
    print("Starting Solar Control...")
    print(f"Gateway API Key configured: {settings.api_key[:4]}...")
    
    # Refresh model registry
    await gateway.refresh_model_registry()
    print("Model registry initialized")
    print("Solar Control started successfully")
    
    yield
    
    # Shutdown
    print("Shutting down Solar Control...")
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


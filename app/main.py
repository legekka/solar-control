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
    lifespan=lifespan
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
    """Verify API key for all requests except health check"""
    if request.url.path == "/health":
        return await call_next(request)
    
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != settings.api_key:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or missing API key"}
        )
    
    return await call_next(request)


# Include routers
app.include_router(hosts.router)
app.include_router(openai.router)
app.include_router(websockets.router)


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


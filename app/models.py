from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Any, Dict
from datetime import datetime, timezone
from enum import Enum


class HostStatus(str, Enum):
    """Status of a solar-host"""
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"


class MemoryInfo(BaseModel):
    """Memory usage information"""
    used_gb: float = Field(..., description="Used memory in GB")
    total_gb: float = Field(..., description="Total memory in GB")
    percent: float = Field(..., description="Usage percentage")
    memory_type: str = Field(..., description="Type of memory (VRAM or RAM)")


class Host(BaseModel):
    """Solar host information"""
    id: str
    name: str
    url: str
    api_key: str
    status: HostStatus = HostStatus.OFFLINE
    last_seen: Optional[datetime] = None
    memory: Optional[MemoryInfo] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HostCreate(BaseModel):
    """Request to register a new host"""
    name: str
    url: str
    api_key: str


class HostResponse(BaseModel):
    """Response for host operations"""
    host: Host
    message: str


class ModelInfo(BaseModel):
    """Model information for OpenAI compatibility"""
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    owned_by: str = "solar"


class ModelsResponse(BaseModel):
    """OpenAI /v1/models response"""
    object: str = "list"
    data: List[ModelInfo]


class ChatMessage(BaseModel):
    """Chat message"""
    model_config = ConfigDict(extra='allow')
    
    role: str
    content: str
    name: Optional[str] = None  # Optional name field for OpenAI compatibility


class ChatCompletionRequest(BaseModel):
    """OpenAI chat completion request"""
    model_config = ConfigDict(extra='allow')
    
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    n: Optional[int] = None
    stop: Optional[List[str]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    # Allows pass-through of additional OpenAI parameters


class CompletionRequest(BaseModel):
    """OpenAI completion request"""
    model_config = ConfigDict(extra='allow')
    
    model: str
    prompt: str
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    n: Optional[int] = None
    stop: Optional[List[str]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    # Allows pass-through of additional OpenAI parameters


class ProxyRequest(BaseModel):
    """Generic proxy request"""
    endpoint: str
    method: str = "POST"
    data: Optional[Dict[str, Any]] = None


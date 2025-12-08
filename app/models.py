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
    created: int = Field(
        default_factory=lambda: int(datetime.now(timezone.utc).timestamp())
    )
    owned_by: str = "solar"


class ModelsResponse(BaseModel):
    """OpenAI /v1/models response"""

    object: str = "list"
    data: List[ModelInfo]


class ChatMessage(BaseModel):
    """Chat message"""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str
    name: Optional[str] = None  # Optional name field for OpenAI compatibility


class ChatCompletionRequest(BaseModel):
    """OpenAI chat completion request"""

    model_config = ConfigDict(extra="allow")

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

    model_config = ConfigDict(extra="allow")

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


# Classification models for HuggingFace SequenceClassification backend


class ClassifyRequest(BaseModel):
    """Classification request for HuggingFace classification models"""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any = Field(..., description="Text or list of texts to classify")
    return_all_scores: bool = Field(
        default=False,
        description="Return scores for all classes, not just top prediction",
    )


class ClassifyScoreItem(BaseModel):
    """Individual class score."""

    label: str
    score: float


class ClassifyChoice(BaseModel):
    """Classification result for a single input"""

    index: int
    label: str
    score: float
    all_scores: Optional[List[ClassifyScoreItem]] = Field(
        default=None, description="Scores for all classes (when return_all_scores=True)"
    )


class ClassifyResponse(BaseModel):
    """Classification response"""

    id: str
    object: str = "classification"
    model: str
    choices: List[ClassifyChoice]
    usage: Dict[str, int]


# Embedding models for HuggingFace embedding backend


class EmbeddingRequest(BaseModel):
    """OpenAI-compatible embedding request for HuggingFace embedding models"""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any = Field(..., description="Text or list of texts to embed")
    encoding_format: Optional[str] = Field(
        default="float", description="Encoding format: 'float' or 'base64'"
    )
    dimensions: Optional[int] = Field(
        default=None, description="Optional dimension truncation"
    )


class EmbeddingData(BaseModel):
    """Individual embedding result"""

    object: str = "embedding"
    embedding: List[float]
    index: int


class EmbeddingResponse(BaseModel):
    """OpenAI-compatible embedding response"""

    object: str = "list"
    data: List[EmbeddingData]
    model: str
    usage: Dict[str, int]


# WebSocket 2.0 Message Types


class WSMessageType(str, Enum):
    """WebSocket message types for the unified protocol"""

    # Host -> Control messages
    REGISTRATION = "registration"
    LOG = "log"
    INSTANCE_STATE = "instance_state"
    HOST_HEALTH = "host_health"

    # Control -> WebUI messages (also used internally)
    HOST_STATUS = "host_status"
    INITIAL_STATUS = "initial_status"
    REQUEST_START = "request_start"
    REQUEST_ROUTED = "request_routed"
    REQUEST_SUCCESS = "request_success"
    REQUEST_ERROR = "request_error"
    REQUEST_REROUTE = "request_reroute"
    KEEPALIVE = "keepalive"


class WSMessage(BaseModel):
    """Base WebSocket message envelope"""

    type: WSMessageType
    host_id: Optional[str] = None
    instance_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Dict[str, Any] = Field(default_factory=dict)


class WSRegistration(BaseModel):
    """Host registration message data"""

    host_id: str
    api_key: str
    host_name: Optional[str] = None
    instances: List[Dict[str, Any]] = Field(default_factory=list)


class WSLogMessage(BaseModel):
    """Log message from host"""

    seq: int
    line: str
    level: Optional[str] = None


class WSInstanceState(BaseModel):
    """Instance runtime state update"""

    busy: bool = False
    phase: Optional[str] = None
    prefill_progress: Optional[float] = None
    active_slots: int = 0
    slot_id: Optional[int] = None
    task_id: Optional[int] = None
    prefill_prompt_tokens: Optional[int] = None
    generated_tokens: Optional[int] = None
    decode_tps: Optional[float] = None
    decode_ms_per_token: Optional[float] = None
    checkpoint_index: Optional[int] = None
    checkpoint_total: Optional[int] = None


class WSHostHealth(BaseModel):
    """Host health/memory update"""

    memory: Optional[MemoryInfo] = None
    instance_count: int = 0
    running_instance_count: int = 0

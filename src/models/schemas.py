from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


# --- Request Models ---

class DocumentCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=1024)
    content: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    author: Optional[str] = None
    file_type: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


# --- Response Models ---

class DocumentResponse(BaseModel):
    id: UUID
    title: str
    content: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    author: Optional[str] = None
    file_type: Optional[str] = None
    status: str
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: Optional[datetime] = None


class DocumentCreateResponse(BaseModel):
    id: UUID
    status: str = "processing"
    message: str = "Document queued for indexing"
    created_at: datetime


class DocumentDeleteResponse(BaseModel):
    id: UUID
    status: str = "deleted"
    message: str = "Document deletion queued"


class SearchHighlight(BaseModel):
    title: list[str] = Field(default_factory=list)
    content: list[str] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    id: str
    title: str
    score: float
    highlights: SearchHighlight = Field(default_factory=SearchHighlight)
    author: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None


class FacetBucket(BaseModel):
    key: str
    count: int


class SearchResponse(BaseModel):
    query: str
    total_hits: int
    page: int
    size: int
    took_ms: int
    results: list[SearchResultItem]
    facets: dict[str, list[FacetBucket]] = Field(default_factory=dict)


class DependencyHealth(BaseModel):
    status: str
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    timestamp: datetime
    dependencies: dict[str, DependencyHealth]


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorResponse(BaseModel):
    error: ErrorDetail

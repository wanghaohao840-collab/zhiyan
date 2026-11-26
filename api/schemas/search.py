from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=1000)
    document_ids: list[str] = Field(min_length=1, max_length=10)
    limit: Literal[5, 10, 20] = 10


class ChunkLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    chunk_id: str = Field(min_length=1, max_length=256)
    chunk_index: int = Field(ge=0, strict=True)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int | None = Field(default=None, ge=1, strict=True)


class SearchHit(BaseModel):
    document_id: str
    document_name: str
    excerpt: str
    rank: int
    score: float
    page_number: int | None
    section: str | None
    locator: ChunkLocator


class SearchResponse(BaseModel):
    request_id: str
    document_ids: list[str]
    result_count: int
    results: list[SearchHit]

"""User-reviewable semantic memory with explicit creation and deletion."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from .tiers import SemanticMemory

router = APIRouter(prefix="/api/memory")
semantic = SemanticMemory()


class FactInput(BaseModel):
    category: Literal["personal", "retail", "coding"]
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=1000)
    ttl_days: int = Field(default=90, ge=1, le=365, alias="ttlDays")
    model_config = {"populate_by_name": True}


@router.get("/facts")
def facts(category: str | None = None):
    if category is not None and category not in SemanticMemory.CATEGORIES:
        raise HTTPException(422, "invalid category")
    return semantic.recall(category)


@router.post("/facts", status_code=201)
def remember(body: FactInput):
    try:
        identifier = semantic.remember(body.category, body.key, body.value, ttl_days=body.ttl_days)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"id": identifier}


@router.delete("/facts/{identifier}")
def forget(identifier: str):
    if not semantic.forget(identifier):
        raise HTTPException(404, "fact not found")
    return {"ok": True}

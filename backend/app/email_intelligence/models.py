"""Data models for Autonomous Email Intelligence."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


EmailCategory = Literal["unnecessary", "important", "uncertain"]
EmailSubcategory = Literal[
    "promotional", "newsletter", "notification", "recruitment",
    "interview", "education", "financial", "security", "personal", "unknown"
]
EmailActionTaken = Literal["none", "dry_run_flagged", "moved_to_trash", "restored"]


class EmailItem(BaseModel):
    id: str
    thread_id: str = ""
    sender: str
    recipient: str = ""
    subject: str = ""
    snippet: str = ""
    body_text: str = ""
    received_at: float = Field(default_factory=time.time)
    is_unread: bool = True
    is_replied: bool = False
    labels: list[str] = Field(default_factory=list)
    raw_headers: dict[str, str] = Field(default_factory=dict)


class EmailClassificationResult(BaseModel):
    category: EmailCategory
    subcategory: EmailSubcategory = "unknown"
    importance_score: float = Field(ge=0.0, le=1.0, default=0.0)
    unnecessary_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning: str = ""
    deadline: Optional[str] = None
    required_action: Optional[str] = None
    recommended_action: str = "Review email"


class CleanupPolicy(BaseModel):
    allow_promotions: bool = True
    allow_newsletters: bool = True
    allow_notifications: bool = True
    min_confidence: float = 0.80
    protected_categories: list[str] = Field(
        default_factory=lambda: [
            "financial", "security", "education", "recruitment", "personal"
        ]
    )


class NotificationPolicy(BaseModel):
    notify_deadlines: bool = True
    notify_interviews: bool = True
    notify_unanswered_important: bool = True
    neglect_threshold_seconds: int = 3600  # 1 hour
    custom_keywords: list[str] = Field(default_factory=list)


class EmailMissionSetupInput(BaseModel):
    account_email: str
    provider: Literal["gmail", "outlook"] = "gmail"
    frequency_seconds: int = Field(default=300, ge=60, le=86400)
    cleanup_policy: CleanupPolicy = Field(default_factory=CleanupPolicy)
    notification_policy: NotificationPolicy = Field(default_factory=NotificationPolicy)
    conversation_id: Optional[str] = None


from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import config


class TaskInput(BaseModel):
    instruction: str = Field(min_length=1, max_length=config.MAX_INSTRUCTION_CHARS)
    chat_id: Optional[str] = Field(default=None, alias="chatId")
    file_token: Optional[str] = Field(default=None, alias="fileToken")
    # Task 3: optional per-task domain allow-list. None = inherit the
    # global PRIVAGENT_ALLOWED_DOMAINS list. A list (even an empty one,
    # which means "no restriction for this task") overrides the global.
    allowed_domains: Optional[list[str]] = Field(default=None, alias="allowedDomains")
    # Task 9: long_horizon tasks get the 100-step cap + history
    # compaction; background tasks run without a live WebSocket client
    # and are pollable via GET /api/task/{id}. Neither flag weakens any
    # gate.
    long_horizon: bool = Field(default=False, alias="longHorizon")
    background: bool = False
    model_config = {"populate_by_name": True}

    @field_validator("instruction")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction must not be blank")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def _valid_domains(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        # parse_allowed_domains validates each entry and de-duplicates;
        # a bad entry raises ValueError -> 422 from the endpoint.
        from .allowlist import parse_allowed_domains

        return parse_allowed_domains(value)


class ChatCreateInput(BaseModel):
    title: Optional[str] = None


class ChatMessageInput(BaseModel):
    instruction: Optional[str] = None
    content: Optional[str] = None
    file_token: Optional[str] = Field(default=None, alias="fileToken")

    @property
    def text(self) -> str:
        val = (self.instruction or self.content or "").strip()
        if not val:
            raise ValueError("instruction or content must not be blank")
        return val


class ConfirmInput(BaseModel):
    # Task 12: "approve_step" approves the pending step exactly like
    # "approve", and additionally tells the loop not to run the rest
    # of the validated batch without a fresh plan — the operator
    # wants to see each step's preview. It never overrides a gate.
    decision: Literal["approve", "approve_step", "reject"]


class VaultCredentialInput(BaseModel):
    """Task 4: one credential to store in the encrypted vault.

    ``value`` is the secret itself (password, or a card's last 4 digits
    only). It is accepted once over the authenticated API, encrypted
    into the Task 2 store, and never returned by any endpoint — the
    list endpoint returns metadata (domain/kind/label/placeholder)
    only. OTP / one-time-code kinds are rejected by the vault, not by
    this schema, so the rejection reason stays in one place.
    """

    domain: str = Field(min_length=1, max_length=253)
    kind: Literal["login", "card"]
    label: str = Field(min_length=1, max_length=64)
    username: Optional[str] = Field(default=None, max_length=320)
    value: str = Field(min_length=1, max_length=4096)


class PaymentValidateInput(BaseModel):
    """Task 4: ask whether a payment would be allowed (no charge)."""

    amount: float
    currency: Optional[str] = None
    merchant_domain: str = Field(min_length=1, max_length=253, alias="merchantDomain")
    model_config = {"populate_by_name": True}

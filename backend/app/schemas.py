from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import config


class TaskInput(BaseModel):
    instruction: str = Field(min_length=1, max_length=config.MAX_INSTRUCTION_CHARS)
    chat_id: Optional[str] = Field(default=None, alias="chatId")
    file_token: Optional[str] = Field(default=None, alias="fileToken")
    model_config = {"populate_by_name": True}

    @field_validator("instruction")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction must not be blank")
        return value


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
    decision: Literal["approve", "reject"]


class ProgressEnvelope(BaseModel):
    type: str
    taskId: str
    payload: dict

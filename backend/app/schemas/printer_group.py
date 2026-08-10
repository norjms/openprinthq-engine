"""Schemas for printer groups."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class PrinterGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=20)
    position: int = 0
    printer_ids: list[int] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        stripped = (v or "").strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class PrinterGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=20)
    position: int | None = None
    # None means "leave membership alone"; an empty list means "remove all".
    printer_ids: list[int] | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class PrinterGroupMember(BaseModel):
    id: int
    name: str
    model: str | None = None
    location: str | None = None
    connection_type: str | None = None
    is_active: bool = True


class PrinterGroupResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    color: str | None = None
    position: int = 0
    printer_count: int = 0
    printers: list[PrinterGroupMember] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

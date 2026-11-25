from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.services.location_service import normalize_location_name
from backend.app.utils.location_codes import is_location_code, normalize_location_code

LocationKind = Literal["storage", "drybox", "printer", "ams", "ams_slot", "external", "other"]


def _validate_code(v: str | None) -> str | None:
    if v is None:
        return None
    code = normalize_location_code(v)
    if not is_location_code(code):
        raise ValueError("code must be 'L' followed by 6 Crockford base32 characters")
    return code


class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    identifier: str | None = Field(default=None, max_length=100)
    kind: LocationKind = "storage"
    parent_id: int | None = None
    printer_id: int | None = None
    ams_id: int | None = Field(default=None, ge=0, le=255)
    tray_id: int | None = Field(default=None, ge=0, le=3)
    # Generated ("L" + 6 Crockford base32) when absent.
    code: str | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return normalize_location_name(v)

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        return _validate_code(v)


class LocationUpdate(BaseModel):
    """Partial update. Only fields present in the request body are applied, so
    ``{"parent_id": null}`` clears the parent while omitting it leaves it."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    identifier: str | None = Field(default=None, max_length=100)
    kind: LocationKind | None = None
    parent_id: int | None = None
    printer_id: int | None = None
    ams_id: int | None = Field(default=None, ge=0, le=255)
    tray_id: int | None = Field(default=None, ge=0, le=3)
    code: str | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_location_name(v)

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        return _validate_code(v)


class LocationResponse(BaseModel):
    id: int
    name: str
    identifier: str | None = None
    kind: str = "storage"
    parent_id: int | None = None
    printer_id: int | None = None
    ams_id: int | None = None
    tray_id: int | None = None
    code: str | None = None
    note: str | None = None
    spool_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LocationWithSpoolsResponse(LocationResponse):
    """A location plus the spools stored at it.

    ``spools`` holds internal spool ids in local-inventory mode and Spoolman
    spool ids in Spoolman mode (``spool_source`` says which).
    """

    spool_source: Literal["internal", "spoolman"] = "internal"
    spools: list[int] = Field(default_factory=list)
    printer_name: str | None = None

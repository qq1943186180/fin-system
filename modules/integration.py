"""Shared, serialisable result type for external integrations."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator


class IntegrationStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_REJECTED = "business_rejected"
    RETRYABLE_ERROR = "retryable_error"
    CONFIG_ERROR = "config_error"


@dataclass
class IntegrationResult:
    status: IntegrationStatus
    message: str
    data: Any = None
    request_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is IntegrationStatus.SUCCESS

    def __iter__(self) -> Iterator[Any]:
        """Compatibility with the former ``(ok, message, data)`` tuple API."""
        yield self.ok
        yield self.message
        yield self.data

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "message": self.message,
            "data": self.data,
            "request_id": self.request_id,
            "details": self.details,
        }


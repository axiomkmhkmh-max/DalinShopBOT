"""Supplier abstraction. No real supplier API is assumed to exist."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from config import get_config
from db import get_db


class SupplierError(Exception):
    pass


@dataclass
class SupplierRequest:
    order_id: str
    product: dict
    quantity: int
    user_id: int


@dataclass
class SupplierResult:
    status: str                 # DELIVERED | PENDING | FAILED
    message: str = ""
    external_ref: str = ""
    delivery_text: str = ""     # optional text shown to the customer (e.g. a code)


class Supplier(ABC):
    key = "abstract"

    @abstractmethod
    async def place_order(self, req: SupplierRequest) -> SupplierResult: ...


class ManualSupplier(Supplier):
    """Admin delivers by hand from the dashboard. The order stays PROCESSING until then."""
    key = "manual"

    async def place_order(self, req: SupplierRequest) -> SupplierResult:
        return SupplierResult("PENDING", "manual fulfillment required")


class MockSupplier(Supplier):
    """TEST/DEV ONLY."""
    key = "mock"

    def __init__(self, mode: str = "deliver"):
        self.mode = mode
        self.calls = 0

    async def place_order(self, req: SupplierRequest) -> SupplierResult:
        self.calls += 1
        if self.mode == "raise":
            raise SupplierError("mock supplier exception")
        if self.mode == "fail":
            return SupplierResult("FAILED", "mock supplier failure")
        if self.mode == "pending":
            return SupplierResult("PENDING", "mock pending", external_ref=f"mock-{req.order_id[:8]}")
        return SupplierResult("DELIVERED", "ok", external_ref=f"mock-{req.order_id[:8]}")


class ApiSupplier(Supplier):
    """Placeholder for a real supplier API. You must implement it once you have a real, documented API."""
    key = "api"

    async def place_order(self, req: SupplierRequest) -> SupplierResult:
        raise SupplierError("API supplier is not configured")


_override: dict[str, Supplier] = {}


def register_override(key: str, supplier: Supplier) -> None:   # used by tests
    _override[key] = supplier


async def get_supplier(key: str | None) -> Supplier:
    key = key or "manual"
    if key in _override:
        return _override[key]
    if key == "manual":
        return ManualSupplier()
    doc = await get_db().suppliers.find_one({"key": key})
    if not doc or not doc.get("enabled"):
        raise SupplierError("supplier disabled or missing")
    if doc["type"] == "mock":
        if get_config().is_production:
            raise SupplierError("mock supplier forbidden in production")
        return MockSupplier()
    if doc["type"] == "manual":
        return ManualSupplier()
    return ApiSupplier()

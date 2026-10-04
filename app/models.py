"""Domain model enums and records for the procurement workflow."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RequisitionStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    QUOTING = "QUOTING"
    QUOTE_CLOSED = "QUOTE_CLOSED"
    APPROVING = "APPROVING"
    APPROVED = "APPROVED"
    ORDERED = "ORDERED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class POStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FULFILLED = "FULFILLED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class QuoteStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    LATE = "LATE"
    SUPERSEDED = "SUPERSEDED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TRANSFERRED = "TRANSFERRED"


class SupplierStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class Role(str, Enum):
    BUYER = "BUYER"
    MANAGER = "MANAGER"
    DIRECTOR = "DIRECTOR"


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str
    status: SupplierStatus


@dataclass(frozen=True)
class Requisition:
    id: str
    title: str
    amount: float
    status: RequisitionStatus
    version: int


@dataclass(frozen=True)
class Quote:
    id: str
    requisition_id: str
    supplier_id: str
    amount: float
    valid_until: str
    status: QuoteStatus


@dataclass(frozen=True)
class Approval:
    id: str
    requisition_id: str
    step: int
    role: Role
    assignee: str
    status: ApprovalStatus


@dataclass(frozen=True)
class PurchaseOrder:
    id: str
    requisition_id: str
    supplier_id: str
    amount: float
    status: POStatus
    version: int


@dataclass(frozen=True)
class POVersion:
    po_id: str
    version: int
    payload: dict = field(default_factory=dict)

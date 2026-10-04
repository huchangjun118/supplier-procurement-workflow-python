"""Declarative transition tables: a status may only advance along these edges."""
from __future__ import annotations

from .errors import Reason, WorkflowError
from .models import POStatus, RequisitionStatus

REQUISITION_TRANSITIONS: dict[RequisitionStatus, set[RequisitionStatus]] = {
    RequisitionStatus.DRAFT: {RequisitionStatus.SUBMITTED, RequisitionStatus.CANCELLED},
    RequisitionStatus.SUBMITTED: {RequisitionStatus.QUOTING, RequisitionStatus.CANCELLED},
    RequisitionStatus.QUOTING: {RequisitionStatus.QUOTE_CLOSED, RequisitionStatus.CANCELLED},
    RequisitionStatus.QUOTE_CLOSED: {RequisitionStatus.APPROVING},
    RequisitionStatus.APPROVING: {RequisitionStatus.APPROVED, RequisitionStatus.REJECTED},
    RequisitionStatus.APPROVED: {RequisitionStatus.ORDERED},
    RequisitionStatus.ORDERED: {RequisitionStatus.COMPLETED},
    RequisitionStatus.COMPLETED: set(),
    RequisitionStatus.REJECTED: set(),
    RequisitionStatus.CANCELLED: set(),
}

PO_TRANSITIONS: dict[POStatus, set[POStatus]] = {
    POStatus.DRAFT: {POStatus.SUBMITTED, POStatus.CANCELLED},
    POStatus.SUBMITTED: {POStatus.CONFIRMED, POStatus.CANCELLED},
    POStatus.CONFIRMED: {POStatus.FULFILLED},
    POStatus.FULFILLED: {POStatus.CLOSED},
    POStatus.CLOSED: set(),
    POStatus.CANCELLED: set(),
}


def can_transition(table: dict, current, target) -> bool:
    return target in table.get(current, set())


def assert_transition(table: dict, current, target, entity: str, entity_id: str) -> None:
    """Raise a distinguishable error when a jump is not in the table."""
    if current == target:
        raise WorkflowError(
            Reason.DUPLICATE_SUBMISSION,
            f"{entity} {entity_id} is already {current.value}",
        )
    if not can_transition(table, current, target):
        raise WorkflowError(
            Reason.ILLEGAL_TRANSITION,
            f"{entity} {entity_id}: {current.value} -> {target.value} is not allowed",
        )

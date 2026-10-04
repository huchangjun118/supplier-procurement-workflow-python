"""工作流错误定义。

每类非法操作都有独立的、可区分的错误码 (code)，便于调用方分支处理与审计回溯。
"""

from __future__ import annotations


class WorkflowError(Exception):
    """所有工作流业务错误的基类。"""

    code: str = "WORKFLOW_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return f"[{self.code}] {self.message}"


class IllegalTransitionError(WorkflowError):
    """状态机非法跳转（当前状态不允许执行该动作）。"""

    code = "ILLEGAL_TRANSITION"


class PreconditionFailedError(WorkflowError):
    """动作前置条件不满足（如缺少有效报价、审批链未完结等）。"""

    code = "PRECONDITION_FAILED"


class UnauthorizedApprovalError(WorkflowError):
    """审批人无权审批当前节点（角色不匹配 / 越权审批）。"""

    code = "UNAUTHORIZED_APPROVAL"


class QuoteRejectedError(WorkflowError):
    """报价被拒绝（供应商未准入/已停用、需求不在报价阶段、超过截止时间等）。"""

    code = "QUOTE_REJECTED"


class DuplicateQuoteError(WorkflowError):
    """同一供应商对同一需求重复报价。"""

    code = "DUPLICATE_QUOTE"


class DuplicateSubmitError(WorkflowError):
    """同一业务实体（需求/采购单/供应商）重复创建提交。"""

    code = "DUPLICATE_SUBMIT"


class SupplierNotFoundError(WorkflowError):
    code = "SUPPLIER_NOT_FOUND"


class SupplierNotAdmittedError(WorkflowError):
    """供应商未通过准入，不能参与新业务。"""

    code = "SUPPLIER_NOT_ADMITTED"


class SupplierDisabledError(WorkflowError):
    """供应商已停用，不能参与新业务。"""

    code = "SUPPLIER_DISABLED"


class ConflictError(WorkflowError):
    """并发冲突：提交基于的版本已过期（乐观锁）。"""

    code = "VERSION_CONFLICT"


class VersionNotFoundError(WorkflowError):
    """请求回滚/查看的历史版本不存在。"""

    code = "VERSION_NOT_FOUND"


class EntityNotFoundError(WorkflowError):
    """业务实体不存在。"""

    code = "ENTITY_NOT_FOUND"


class TransferError(WorkflowError):
    """审批转交非法（转交给无权审批该节点的人等）。"""

    code = "TRANSFER_INVALID"

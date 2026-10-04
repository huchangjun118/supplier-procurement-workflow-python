# Supplier Procurement Workflow

采购需求（Requisition）→ 供应商报价（Quote）→ 分级审批（Approval）→ 采购单（PurchaseOrder）
的协同工作流。所有状态、审批、报价与单据变更收敛在一套可解释、可回溯的流程里。

## 运行

```bash
uv run pytest tests/          # 运行测试
uv run python main.py         # 启动 HTTP 服务（默认 workflow.db）
```

## 流程语义

### 1. 状态机（只能按既定顺序推进）

```
需求: DRAFT → SUBMITTED → QUOTING → QUOTE_CLOSED → APPROVING → APPROVED → ORDERED → COMPLETED
                ↘ CANCELLED（DRAFT/SUBMITTED/QUOTING 可取消）   ↘ REJECTED（审批拒绝）
采购单: DRAFT → SUBMITTED → CONFIRMED → FULFILLED → CLOSED（DRAFT/SUBMITTED 可 CANCELLED）
```

- 非法跳转返回 `ILLEGAL_TRANSITION`；对同一状态重复推进返回 `DUPLICATE_SUBMISSION`；
  前置条件未满足（如报价窗口未开、无有效报价可裁决）返回 `PRECONDITION_MISSING`。
- 每个变更接口接受 `Idempotency-Key` 请求头：同一 key 的重试直接回放首个已提交结果；
  同一 key 携带不同负载返回 `IDEMPOTENCY_CONFLICT`。

### 2. 报价裁决

- 同一需求下多供应商可并发提交报价；同一供应商重复报价返回 `QUOTE_DUPLICATE`。
- 关窗裁决规则：**金额最低者中标，金额相同则先到达者（提交序号小）中标**；
  `valid_until` 早于裁决时刻的报价比记为 `LATE` 且不参与。
- 关窗后迟到的报价返回 `QUOTE_WINDOW_CLOSED`，已确定的结果不会偏移。
- 裁决依据（规则、候选、胜者）写入审计事件 `quotes_adjudicated`，可经
  `GET /events/requisition/{id}` 查询说明。

### 3. 分级审批

- 审批链按**中标金额**分档生成（见配置项），逐级审批，全部通过后需求进入 `APPROVED`。
- 越权（角色不符或不是指派人）返回 `APPROVAL_FORBIDDEN`；对非 PENDING 节点操作返回
  `APPROVAL_NOT_PENDING`。并发审批由数据库事务串行化，恰好一个提交生效。
- 审批人离职/变更：`POST /approvals/{id}/transfer` 将 PENDING 节点转交，旧记录标记
  `TRANSFERRED` 保留在链上，新节点继承同一 step，审批链完整可追溯。

### 4. 采购单版本化

- 采购单每次创建/推进/变更都会写入 `po_versions` 历史快照。
- 变更采用**整文档乐观并发**：`amend` 携带 `base_version`，并发冲突时先到者生效，
  后者收到 `VERSION_CONFLICT`，绝不出现字段级混合。
- `rollback` 把历史版本复制为**新版本**（历史不丢失），回滚后行为与该版本当时一致。

### 5. 失败与恢复

- 每个服务方法是一个 `BEGIN IMMEDIATE` 事务：任一步失败整体回滚，不留部分写入。
- 重启后已提交的状态（status 列）与审计事件原样可见，流程从最近已提交位置继续；
  未完成的节点（PENDING 审批、DRAFT 采购单）与已提交结果天然可区分。

### 6. 供应商准入与停用

- `PENDING → ACTIVE → SUSPENDED → ACTIVE`（admit / suspend / reinstate）。
- 未准入报价返回 `SUPPLIER_NOT_ADMITTED`；已停用报价/新建采购单返回 `SUPPLIER_SUSPENDED`
  （下单时会重新校验供应商状态）。
- 停用不影响历史单据：已存在的报价与采购单仍可查询，状态变更写入审计事件。

## 配置项

| 配置 | 位置 | 说明 |
| --- | --- | --- |
| `db_path` | `create_app(db_path=...)` | SQLite 路径，`:memory:` 为纯内存 |
| `ApprovalPolicy.tiers` | `create_app(approval_policy=...)` | `(金额上限, 角色序列)` 列表，首个覆盖中标金额的档定义审批链；默认 `(1万→[MANAGER])`、`(10万→[MANAGER, DIRECTOR])`、`(∞→[MANAGER, DIRECTOR])` |
| `Idempotency-Key` | 请求头 | 所有写接口的幂等键 |

## 主要接口

```
POST /suppliers            POST /suppliers/{id}/admit|suspend|reinstate
POST /requisitions         POST /requisitions/{id}/transition
POST /requisitions/{id}/quotes            POST /requisitions/{id}/close-quoting
POST /requisitions/{id}/approvals         POST /approvals/{id}/approve|reject|transfer
POST /requisitions/{id}/purchase-orders   POST /purchase-orders/{id}/transition|amend|rollback
GET  /purchase-orders/{id}/versions       GET  /events/{entity}/{id}
```

错误响应统一为 `409 {"reason": <机器可区分的原因码>, "detail": ...}`，
原因码见 `app/errors.py` 的 `Reason` 枚举。

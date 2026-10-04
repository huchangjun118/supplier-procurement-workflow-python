# 采购需求 → 供应商采购单 协同工作流

把**状态流转、供应商报价、分级审批、采购单变更**收敛到一套基于事件日志（WAL）的、
可解释、可恢复的确定性流程中，解决以下问题：

- 流转状态停在中间、非法跳转；
- 审批与报价并发时重复下单、越权审批；
- 迟到/重复报价使已确定的采购结果偏移；
- 采购单并发变更出现字段级混合；
- 中断/失败后留下部分写入，重启无法区分已提交与未完成。

## 1. 架构总览

```
HTTP (procurement.api, FastAPI)
        │
ProcurementService   事务模板 / 幂等键 / 乐观锁基线 / 只读查询视图
        │
WorkflowEngine        纯领域状态机：校验 -> 产生事件 -> apply 投影（执行与重放同一路径）
        │
Journal               两阶段 WAL：prepare(event 行) -> commit(标记行)，线程安全串行化
        │
procurement.wal       崩溃恢复的唯一事实来源
```

- 所有写操作都在**单临界区事务**内完成：在工作副本（深拷贝）上执行命令，
  事件先逐行落盘（prepare），成功后追加 `commit` 标记，再把结果并入正式状态；
- 命令抛异常 → 不写 `commit`，已 prepare 的事件对任何人不可见，重放时丢弃，
  因此**不会有部分写入的中间状态**；
- 重启调用 `ProcurementService.recover()`：重放 WAL 重建内存投影，并给出
  「已提交 / 停在中间可继续」的盘点报告。

## 2. 状态机与流转语义

采购需求状态（非法跳转返回 `ILLEGAL_TRANSITION`，缺前置条件返回独立错误码）：

```
DRAFT ──submit──▶ SUBMITTED ──open_quoting──▶ QUOTING ──close_quoting──▶ AWARDED
   │                  │(可取消)                   │  报价并发进入
   ▼                  ▼                           ▼
CANCELLED        CANCELLED                   close 时按规则裁决
                                                         │
                                              start_approval
                                                         ▼
                                                     APPROVING ──全部通过──▶ APPROVED
                                                         │                       │
                                                  任一节点驳回               create_order
                                                         ▼                       ▼
                                                     REJECTED                ORDERED
                                                                                 │
                                                                          complete_order
                                                                                 ▼
                                                                              CLOSED
任意活跃阶段可 cancel ──▶ CANCELLED（ORDERED 之后不可取消，需走采购单完结/变更）
```

| 当前状态 | 允许动作 | 下一状态 |
| --- | --- | --- |
| DRAFT | submit / cancel | SUBMITTED / CANCELLED |
| SUBMITTED | open_quoting / cancel | QUOTING / CANCELLED |
| QUOTING | 供应商报价 / close_quoting / cancel | AWARDED / CANCELLED |
| AWARDED | start_approval / cancel | APPROVING / CANCELLED |
| APPROVING | approve / reject / transfer / cancel | APPROVED / REJECTED / CANCELLED |
| APPROVED | create_order / cancel | ORDERED / CANCELLED |
| ORDERED | 变更/回滚采购单 / complete_order | ORDERED / CLOSED |

- 终态：`CLOSED / CANCELLED / REJECTED`，终态上的任何推进动作都被拒绝；
- **幂等**：重复 submit / open / start / approve / complete 返回空结果或
  `idempotent_replay`，不会产生重复事件；携带相同 `Idempotency-Key` 的写请求
  在进程重启后仍可识别（幂等标记随首个事件持久化）。

## 3. 报价与裁决

- 仅 **QUOTING** 阶段、且供应商已准入（`ADMITTED`）时接收报价；
- 同一供应商对同一需求只允许一条有效报价 → 重复返回 `DUPLICATE_QUOTE`；
- 报价必须有未来的 `valid_until`，否则 `QUOTE_VALIDITY_EXPIRED`；
- 显式晚于报价截止时间 → `QUOTE_DEADLINE_PASSED`；
  与截止动作并发竞争落败时可传 `accept_late=True`，报价以 **LATE** 状态留痕但不参与裁决；
- **裁决（close_quoting）规则（确定性、可解释）**：
  1. 仅未过期、按时提交的 `SUBMITTED` 报价有资格；过期报价标记 `REJECTED` 并排除；
  2. 取**最低金额**；金额相同则**先提交者**胜，再相同按供应商编号升序；
  3. 结果保存在 `AwardDecision` 中，`reason` 字段逐条列出每份报价的处置依据；
- 裁决后到达的报价一律 **LATE** 并追加到裁决留痕，**不改变中标结果**，
  后续审批与下单始终指向原中标供应商；
- 没有任何有效报价时拒绝裁决：`NO_ELIGIBLE_QUOTE`。

## 4. 分级审批

内置三级门槛（**累加式**：金额 150,000 需要 manager + director + vp 三节点依次通过）：

| 层级 seq | 角色 | 金额门槛 amount_from | 默认审批人 |
| --- | --- | --- | --- |
| 1 | manager | 0 | alice |
| 2 | director | 10,000 | bob |
| 3 | vp | 100,000 | carol |

- 按**中标金额**生成审批链（不是预估金额）；节点严格按 seq 顺序处理；
- 只有当前节点的指定审批人能处理 → 越权/跳级返回 `UNAUTHORIZED_APPROVAL`，
  错误信息指明要求的角色与当前审批人；
- 节点全部通过 → 需求 `APPROVED`；任一节点驳回（必须填意见）→ 需求 `REJECTED`；
- **转交**：当前审批人可调 `transfer_approval` 给他人（离职/变更）：
  - 必须填原因，不能转给自己，必须由当前审批人发起；
  - 接收人角色**不得低于**本节点（vp 可接 manager 的节点，反之 `TRANSFER_INVALID`）；
  - 节点保留完整 `transfers` 转交链，状态回到 PENDING，不丢失、不跳过；
- 每个节点的通过/驳回/转交都是独立事件，`GET /requisitions/{id}/audit` 可追溯全链。

并发安全：所有动作经 Journal 单锁串行化。同一节点两人同时审批，只有当前审批人成功一次，
另一个收到 `UNAUTHORIZED_APPROVAL`；同一审批人双击，第二次收到冲突/无待办，
`NODE_APPROVED` 事件保证只有一条。

## 5. 采购单版本化

- 需求 `APPROVED` 且中标供应商仍处 `ADMITTED` 时才能 `create_order`；
  重复下单返回 `ALREADY_ORDERED`（幂等键回放除外），从机制上杜绝重复下单；
- 每次 `change_order` 都**追加一个不可变 `OrderVersion`**，历史版本全部保留；
- 仅可改 `amount / item / quantity / expected_delivery / note`，
  未知字段返回 `UNKNOWN_FIELDS`（防止篡改供应商等），变更必须填原因；
- **乐观锁**：变更/回滚必须携带 `expected_version`（HTTP 头 `Expected-Version`）。
  两个基于同一旧版本的并发变更，只有一个成功，另一个在临界区入口即收到
  `VERSION_CONFLICT`，因此**绝不会出现"金额取 A、数量取 B"的字段级混合**；
- `rollback_order(v)`：以历史版本 v 的内容**追加**一个新版本（内容与 v 完全一致，
  `change_reason` 标注 "rollback to vN"）。回滚后当前版本号递增，旧版本号失效，
  后续行为与当初基于 v 完全一致；回滚到不存在版本 → `VERSION_NOT_FOUND`，
  回滚到当前版本 → `ROLLBACK_SAME_VERSION`；
- 采购单仅在需求 `ORDERED` 时可变更/回滚，`CLOSED` 后拒绝。

## 6. 供应商准入/停用

供应商状态：`PENDING → ADMITTED ⇄ DISABLED`；`PENDING → REJECTED`。

- 只有 `ADMITTED` 能参与**新**报价与采购单；
  `PENDING/REJECTED → SUPPLIER_NOT_ADMITTED`，`DISABLED → SUPPLIER_DISABLED`，
  错误信息附带拒绝/停用原因；
- 停用必须填原因；停用走 `disable`、恢复走 `enable`（不能对停用态直接 admit）；
- 已中标但下单前被停用 → `create_order` 被拦截（不向停用供应商下单）；
- **历史单据不受影响**：停用前生成的采购单仍可查询、变更、完结；
  供应商的每次状态变化与原因都记录在 `status_history` 中可查。

## 7. 失败与中断恢复

- WAL 文件路径由环境变量 `PROCUREMENT_WAL` 指定（默认 `./procurement.wal`），
  每行一个 JSON 记录：`{"t":"event",...}`（prepare）/ `{"t":"commit"}`；
- 崩溃落在 prepare 之后、commit 之前的尾部记录在重启时被丢弃；
- `POST /admin/recover`（或 `service.recover()`）返回：

```json
{
  "last_committed_seq": 13,
  "event_count": 13,
  "committed_requisitions": ["..."],      // 已到终态 CLOSED/CANCELLED/REJECTED
  "committed_orders": ["po1"],
  "pending_requisitions": [["r1", "ORDERED", "complete_order（完结采购单）"]],
  "approved_not_ordered": []              // 已审批未下单：最易停住的位置
}
```

- 事件 `seq` 全局连续递增，`pending_requisitions` 直接给出每个中间状态的**下一步动作建议**，
  从最近一次已提交位置继续调用即可。

## 8. 配置项（`procurement.config.WorkflowConfig`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `approval_tiers` | manager/director/vp 三级 | 审批层级；`amount >= amount_from` 的每一级都要审批，按 seq 排序 |
| `default_quote_valid_days` | 30 | 报价默认有效天数 |
| `default_quote_window_hours` | 72 | open_quoting 未给截止时间时的默认窗口 |
| `expired_quote_eligible` | false | 裁决时已过有效期的报价是否仍参与（默认排除） |
| `order_mutable_status` | APPROVED/ORDERED | 允许采购单变更的状态 |

自定义示例：

```python
from procurement.config import ApprovalTier, WorkflowConfig
from procurement.journal import Journal
from procurement.service import ProcurementService

cfg = WorkflowConfig().with_tiers((
    ApprovalTier(1, "staff", 0.0, 1000.0, "u1"),
    ApprovalTier(2, "cfo", 1000.0, None, "u2"),
))
svc = ProcurementService(Journal("procurement.wal"), config=cfg)
```

## 9. 错误码一览

| code | 含义 | 典型 HTTP |
| --- | --- | --- |
| `ILLEGAL_TRANSITION` | 当前状态不允许该动作 | 409 |
| `ALREADY_ORDERED` | 需求已生成采购单，禁止重复下单 | 409 |
| `PRECONDITION_FAILED` | 前置条件不满足（子码如 `NO_ELIGIBLE_QUOTE`/`REASON_REQUIRED`） | 422 |
| `QUOTE_WRONG_PHASE` / `QUOTE_DEADLINE_PASSED` / `QUOTE_VALIDITY_EXPIRED` | 报价阶段/截止/有效期问题 | 422 |
| `DUPLICATE_QUOTE` / `DUPLICATE_SUBMIT` | 重复报价 / 重复创建 | 409 |
| `UNAUTHORIZED_APPROVAL` | 越权或跳级审批 | 403 |
| `TRANSFER_INVALID` | 转交给无权角色/自己等 | 403 |
| `SUPPLIER_NOT_ADMITTED` / `SUPPLIER_DISABLED` | 供应商未准入 / 已停用 | 403 |
| `SUPPLIER_NOT_FOUND` / `REQUISITION_NOT_FOUND` / `ORDER_NOT_FOUND` | 实体不存在 | 404 |
| `VERSION_CONFLICT` | 乐观锁冲突（并发变更） | 409 |
| `VERSION_NOT_FOUND` / `ROLLBACK_SAME_VERSION` | 回滚目标版本问题 | 422 |

## 10. HTTP API 速览（启动后见 `/docs`）

```
POST   /suppliers                      注册供应商（PENDING）
POST   /suppliers/{id}/admit|reject|disable|enable
POST   /requisitions                   创建需求（DRAFT）
POST   /requisitions/{id}/submit
POST   /requisitions/{id}/quoting/open|close
POST   /requisitions/{id}/cancel
POST   /quotes                         提交报价
POST   /requisitions/{id}/approvals/start|approve|reject|transfer
POST   /orders                         生成采购单
POST   /orders/{id}/changes            变更（头 Expected-Version）
POST   /orders/{id}/rollback           回滚（头 Expected-Version）
POST   /orders/{id}/complete
GET    /requisitions|orders|suppliers  列表
GET    /requisitions/{id}/audit        需求完整事件链
GET    /orders/{id}/audit              采购单完整事件链（含全部版本变更）
POST   /admin/recover                  重放 WAL 并输出恢复报告
GET    /admin/events                   全局事件流
```

- 幂等：写请求可带头 `Idempotency-Key: <任意字符串>`，重复键回放首次结果；
- 乐观锁：采购单变更/回滚带头 `Expected-Version: <当前版本>`；
- 错误响应统一为 `{"error": {"code": ..., "message": ...}}`。

## 11. 本地运行与测试

```bash
uv sync
uv run pytest -q               # 60 个用例：状态机/报价并发/审批/版本/恢复/供应商/HTTP
uv run python main.py          # 启动 FastAPI（默认 127.0.0.1:8000，/docs 交互）
```

测试覆盖：并发审批（越权只有一人成功、双击只生效一次）、重复提交幂等、
重复/迟到报价不偏移结果、越权拒绝、审批转交不丢失、采购单并发冲突不字段混合、
版本回滚与当时一致、WAL 崩溃恢复（未提交事务丢弃、重启从提交点继续、幂等跨重启）、
供应商停用对新业务与历史单据的不同影响。

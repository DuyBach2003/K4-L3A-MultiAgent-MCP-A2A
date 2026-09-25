# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống **rule-based, không dùng LLM** (xem `metadata.json`). Mọi kết luận được suy ra
deterministic từ evidence MCP; customer message chỉ dùng để lấy `claimed_order_id`, `claims`
và `policy_version`, không bao giờ được coi là ground truth.

```text
inputs/<case_id>.json
      │
      ▼
Coordinator ──task_assigned──► Order agent ──► get_order, get_order_items
      │   ◄──────handoff──────┘
      ├──task_assigned──► Payment agent ──► get_payment_timeline, get_refund_timeline
      │   ◄──────handoff──────┘
      ├──task_assigned──► Shipment agent ─► get_shipment_summary
      │   ◄──────handoff──────┘
      ├──task_assigned──► Policy agent ───► get_policy → decide() → policy_decided
      │   ◄──────handoff──────┘
      ├──task_assigned──► Order agent ────► get_sellers   (chỉ khi seller chịu trách nhiệm)
      │   ◄──────handoff──────┘
      ├──handoff──► Verifier ──► invariants + JSON Schema → verification_completed
      │   ◄──────handoff──────┘
      ▼
outputs/<case_id>.json + traces/trace.jsonl (case_received … case_finalized)
```

Source: `workflow.py` (coordinator), `agents.py` (specialists), `a2a.py` (bus),
`evidence.py` (ledger + tool runner), `facts.py` (chuẩn hóa evidence), `decision.py`
(rule engine), `verifier.py` (lắp output + kiểm tra).

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case_id`, `claimed_order_id`, `claims`, `policy_version` | Điều phối thứ tự specialist, bỏ qua bước không cần (order không tồn tại → không gọi payment/shipment), lắp output | `task_assigned` cho từng specialist; handoff draft cho verifier |
| Order/item | `order_id` | Trạng thái order, timestamp, item/seller thuộc order; xác nhận seller khi seller bị quy trách nhiệm | `ORDER_SCOPE_READY` / `ORDER_NOT_FOUND`, `SELLER_CONFIRMED` |
| Payment | `order_id` | Capture trong cửa sổ duyệt thanh toán, sự kiện reconciliation, refund trong cửa sổ case | `PAYMENT_RECONCILED` / `PAYMENT_UNAVAILABLE` |
| Shipment | `order_id` | Giao trễ hay không (delivered vs estimated), đối chiếu sự kiện shipment với timestamp order | `DELIVERY_LATE` / `DELIVERY_NOT_LATE` |
| Policy | facts đã chuẩn hóa + `policy_version` | Phân loại `primary_issue`, áp rule policy (status, action, refund, bên chịu trách nhiệm) | `policy_decided` (decision_code = primary_issue), handoff `POLICY_DECIDED` |
| Verifier | draft output + evidence ledger | Kiểm tra invariant (mục 6), sửa lỗi cơ học được, validate schema | `verification_completed` (`PASS`/`REPAIRED`), handoff `READY_TO_FINALIZE` |

Quyền gọi tool (`agents.TOOL_PERMISSIONS`, `ToolRunner` chặn tool ngoài danh sách):

| Actor | Tool được phép |
| --- | --- |
| order-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| coordinator, verifier | không có |

Không agent nào gọi `get_customer_history`, `get_product_context` hay `get_order_payments`:
các domain customer/product không cần cho kết luận, còn `get_payment_timeline` đã chứa đủ
payment row và lifecycle event.

## 3. A2A protocol

- **Envelope** (`a2a.A2AMessage`): `message_id`, `case_id`, `sender`, `recipient`,
  `intent` (task/decision code), `payload`, `evidence_refs`, `hop`.
- **Correlation**: mỗi case có một `A2ABus` và một `EvidenceLedger` riêng; mọi message và
  trace event mang đúng `case_id` của case đó.
- **Handoff**: specialist chỉ handoff sau khi đã fetch xong tool của mình; handoff mang các
  `evidence_refs` mà specialist đã consume.
- **Chống vòng lặp**: luồng tuyến tính, không agent nào tự gọi lại; `MAX_HOPS = 16` trên mỗi
  case (luồng dài nhất dùng 12 hop), vượt quá thì raise lỗi.
- **Timeout**: HTTP timeout 300s đọc / 30s connect (`mcp_gateway.py`); retry theo mục 5.
- **Trace**: chỉ ghi sự kiện quan sát được (actor, target, decision code, tool, evidence ref,
  vài thuộc tính số/chuỗi ngắn); không ghi prompt hay suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate mọi response theo `mcp-evidence-response-v1` trước khi dùng.
2. `ToolRunner.fetch` lưu envelope vào `EvidenceLedger` của case (ref, tool, domain, data) và
   emit `tool_result_consumed` với đúng `evidence_ref` đó.
3. `facts.py` chuẩn hóa data:
   - bỏ các dòng trùng hệt nhau;
   - chỉ giữ capture/reconciliation event trong khoảng `[order_approved_at − 1h, + 24h]`;
   - chỉ giữ item row có `shipping_limit_date` trong `[purchase, estimated_delivery]`;
   - chỉ giữ refund event trong `[purchase, opened_at]`;
   - dòng bị loại được ghi thành `data_conflicts`.
4. `decision.CITATIONS` quy định nhóm evidence cho từng `primary_issue`; output chỉ cite các ref
   có trong ledger của case, không tự tạo hoặc sửa ref.
5. Evidence không bao giờ dùng lại giữa các case: ledger được tạo mới cho mỗi case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi transport | Có, tối đa 3 lần, backoff 1.5s × attempt (đọc idempotent) | Ghi `MCP_UNAVAILABLE`; thiếu order → `insufficient_evidence` | `verification_completed.attributes.tool_failures` |
| Not found / tool trả `isError` | Không | Coi là không có dữ liệu (ví dụ không có refund event) | `ToolFailure(NOT_FOUND/TOOL_ERROR)` |
| Source conflict | Không | Chọn nguồn gắn với timeline của order, ghi `data_conflicts` | `resolution_code` trong output |
| Invalid specialist result / envelope sai schema | Không | Bỏ evidence đó (`INVALID_EVIDENCE`) | `verification_completed` = `REPAIRED` nếu ảnh hưởng output |
| Thiếu rule policy | Không | `needs_investigation`, `escalate_manual_review`, refund 0 | cause `POLICY_RULE_MISSING` |

Không có nhánh nào biến evidence còn thiếu thành dữ liệu phỏng đoán.

## 6. Verification invariants

Kiểm tra trong `verifier.verify` trước khi finalize:

- **Evidence ownership**: mọi `evidence_ref` trong output (kể cả trong claim) phải có trong
  ledger của case; ref lạ bị loại.
- **Claim linkage**: nếu order tồn tại thì evidence của `get_order` phải được cite.
- **Entity scope**: `order_ids` chỉ chứa `claimed_order_id` đã xác minh; seller chịu trách
  nhiệm phải nằm trong `seller_ids`.
- **Money totals**: tổng `refund_lines` = `recommended_refund_brl`.
- **Responsibility/action consistency**: `no_action` ⇒ refund 0; `action_required` ⇒ có ít
  nhất một action; không có action trùng lặp.
- **Confidence bounds**: trong `[0, 1]`.
- **Schema**: validate theo `l3a-output-v2`; CLI validate lại lần nữa khi ghi file.

## 7. Reproducibility

- **Model**: không dùng LLM (rule-based deterministic), khai báo trong `metadata.json`. Tuân
  thủ giới hạn ≤ 10B tham số.
- **Config**: `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`),
  không commit.
- **Dependency**: khoảng version trong `pyproject.toml`; Python 3.11.
- **Concurrency**: tuần tự từng case, một MCP session; khoảng 6–7 MCP call mỗi case; khoảng 5
  phút cho 100 case.
- **Random seed**: không có; chỉ `event_id` và `message_id` là ngẫu nhiên, kết luận
  deterministic.
- **Lệnh chạy**:

  ```bash
  day09 validate-inputs
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```

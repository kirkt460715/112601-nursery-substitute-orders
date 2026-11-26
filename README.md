# 补植订单的替代苗源、现场签收与保活责任

苗源、地段及装车标识都是虚构编号。地段验收范围、批号、到场株数和种植事实分列。保活约定按实际种植日解释。

本地资料台提供合成台账、分批报到的事实以及合作方的独立回执。台账为可供查询的登记记录，回执注明的是合作方所知的实际情况。

履约后端把申领、替代确认、分车收苗与保活责任放在同一份状态里，供采购、现场班组和后续养护共同使用。它只读资料台，不作回写；所有业务事实由调用方显式提交，不存在凭单据自动覆盖已签认苗源的路径。

## 联调入口

使用 Python 3.11.2 标准库。

```sh
python3 -m unittest -v

# 终端一：资料台
python3 evidence_desk.py --port 8765

# 终端二：履约后端（可加 --state state.json 让状态跨重启保留）
python3 fulfillment_server.py --port 8766 --desk 127.0.0.1 8765
```

资料台：

* `GET /register` 读取 `data/register.json`。
* `GET /messages?after=0&size=30` 从 `data/messages.json` 返回一页到达记录。消息含 `sequence`、`received_at`、`source` 和 `facts`。用返回的 `cursor` 读取下一页，游标顺序是接收顺序。
* `GET /partner?action=操作&desk=合作方` 查询 `data/partner-queries.json` 中对应的 `fact`。配置 `unavailable_first` 的查询第一次返回 503，此次查询未能取得回执，同一查询后续可取得已记录的事实。未知查询返回 404。可以用 `--data` 指向另一组同名文件。

## 履约后端

写入接口一律 `POST`，请求体为 JSON；拒绝时返回 `{"error": 稳定错误码, "message": 说明, "details": {...}}`，HTTP 状态按 404（未知对象）、409（状态冲突）、422（规则不允）区分。

### 申领与替代

| 路由 | 说明 |
| --- | --- |
| `POST /orders` | 施工方按地段申领：`site`、可选 `quantity`、可选 `follow_up_of`。数量不得超过地段验收范围。 |
| `GET /orders` | 列出全部申领及状态。 |
| `GET /orders/{id}` | 单张申领的承诺、车次、收苗、种植、缺口与下一步。 |
| `POST /orders/{id}/bids` | 苗圃按自身批次提交供货承诺（`batch`、`quantity`），返回该地段的替代复核视图。 |
| `GET /orders/{id}/substitution-review` | 确认换苗前查看：候选批次的真实可承诺数量、其他申领的占用、本地段已登记的发车。 |
| `POST /bids/{id}/decide` | 批准或驳回：`decision` 为 `approve` / `reject`，可带 `quantity` 部分批准。 |

替代批次只能用于地段许可的来源。批准时按当前批次剩余量扣减；同一替代供给同时申请给两块地时，先批准的一块占用后，另一块在复核视图里看到剩余，超出剩余会被拒并列出竞争承诺。

### 发车与签收

| 路由 | 说明 |
| --- | --- |
| `POST /loads` | 按已批准的承诺派车：`order`、`batch`、`quantity`、`vehicle`。没有对应额度会被拒。 |
| `POST /loads/{id}/reduce` | 苗圃少交：只下调尚未签收的额度，剩余额度仍留在原承诺里可另派一车。 |
| `POST /loads/{id}/close` | 关闭车次，把未交部分退回批次库存。 |
| `POST /loads/{id}/receive` | 班组签收：`vehicle`、`accepted`、`field_signer`，可带 `seen`、`nursery_signer`、`proof_id`、`partner`。车辆不符、超过到场或超过该车待收量都会被拒。 |

`partner` 形如 `{"action": "read-load", "desk": "dispatch-desk"}`，后端会向资料台取回执。首次 503 只记作“未取得回执”，不会被当作已核对；取得的回执若车辆不符或到场少于签收量，拒绝本次签收。

### 苗源凭证与错批

| 路由 | 说明 |
| --- | --- |
| `POST /proofs` | 按批次登记凭证：`batch`、`citation`，可带 `source`、`covers`（限定覆盖的车次）。来源必须与批次登记来源一致。 |
| `GET /proofs` | 按 `batch` 列出凭证。 |
| `GET /proofs/{id}/recheck` | 接到补齐证明后重核：未验批次、待收车次、仍待使用的数量。 |
| `POST /receipts/{id}/mismatch` | 发现错批：`observed_batch`、可选 `note`。已种记录留在原种植点，只加争议标记。 |
| `POST /receipts/{id}/resolve` | 现场处置：`decision` 为 `continue_care` / `top_up` / `return_stock`，可带 `quantity`。 |

缺凭证的实物先登记为 `custody_hold`（临存），不写成已符合合同。后来补的凭证不会自动改写已签认的收苗记录；要继续养护，须由现场对具体记录作处置，处置时才关联补齐的凭证。`top_up` 返回建议的下一轮申领参数，`return_stock` 按可退数量回冲。

### 种植与保活

| 路由 | 说明 |
| --- | --- |
| `POST /receipts/{id}/countersign` | 苗圃会签：`nursery_signer`，与现场签认共同构成保活关联。 |
| `POST /receipts/{id}/plantings` | 登记种植：`row`、`count`、可选 `planted_at` / `site`。 |
| `GET /receipts/{id}/warranty?as_of=` | 单车的保活状态：`not_planted` / `pending_source` / `awaiting_countersign` / `disputed_under_review` / `active` / `expired`。 |
| `POST /receipts/{id}/claims` | 按车发起补植养护，可带 `as_of`。 |
| `GET /orders/{id}/warranty` | 按车列示保活与已发起养护，不合成整单到期日。 |

保活从实际种植日起算，按每一车分别成立：同一地段先签收的正常部分，不会因为后来替代批的争议被整张单算作逾保；争议车本身在处置前不可发起养护。

### 反查缺口

`GET /orders/{id}/gaps` 按 `supply_commitment`、`eligibility_proof`、`physical_delivery`、`care_handover` 四类列明仍缺什么；`GET /orders/{id}` 的 `next_actions` 给出下一步可办的动作，`POST /orders/{id}/close` 关闭本单后可带 `follow_up_of` 开下一轮申领。

## 资料台查询代理

`GET /desk/partner?action=...&desk=...` 与 `GET /desk/messages?sequence=N` 转发到资料台，便于各方在同一服务里核对回执与到达记录。未配置 `--desk` 时返回 503。

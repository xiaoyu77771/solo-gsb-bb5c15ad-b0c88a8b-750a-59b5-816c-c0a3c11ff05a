# 多税区发票金额分摊 API

基于 **FastAPI + SQLite** 的发票金额分摊服务。接收含折扣率、税率与税区比例的发票明细，
全程使用 `Decimal` 精确计算，采用**最大余数法（Largest Remainder / Hare 配额法）**把
舍入差额**稳定地**分配到明细行/税区；税区比例之和不为 1、或提交总额与按明细重算结果
不一致时一律拒绝（HTTP 422）。

## 计算规则

税外发票（tax-exclusive，税额外加），舍入方式 `ROUND_HALF_UP`（四舍五入到分）：

1. **行毛额**：`gross = quantity × unit_price`，乘积必须精确到分，否则拒绝（避免隐式丢厘）。
2. **折扣权重**：`weight = gross × discount_rate`；发票折扣总额 = 各行权重之和再舍入到分。
3. **行折扣分摊（最大余数法①）**：按各行权重把折扣总额（整数分）分摊到行——
   每行先取应摊份额的整数分（floor），剩余的整分按余数从大到小补发；
   **余数相同则按明细行输入顺序补发（稳定 tie-break，结果可复现）**。
4. **行净额**：`net = gross − 行折扣`。
5. **税区净基分摊（最大余数法②）**：行内按 `proportion` 权重把行净额分摊到各税区，
   规则同上；每行税区比例之和必须**严格等于 1**（Decimal 精确比较，不做容差）。
6. **税区税额**：`tax = 分摊净基 × tax_rate`，四舍五入到分。
7. **发票合计**：
   - `gross_total = Σ行毛额`
   - `discount_total = Σ行折扣`（恒等于折扣总额）
   - `net_total = Σ行净基`
   - `tax_total = Σ税区税额`
   - `payable_total = net_total + tax_total`
8. 若请求体带 `expected_totals`，逐字段与重算结果比较，**任一字段不符即拒绝**，
   返回每个不一致字段的期望值/实际值。

返回的每个分摊对象都包含审计依据：

| 字段 | 含义 |
| --- | --- |
| `exact_share` | 未舍入的应摊金额（Decimal 高精度，单位元） |
| `floor` | 向下取整部分（到分） |
| `remainder` | 余数（0 ~ 1 分，决定补发顺序） |
| `allocated` / `allocated_base` | 最终分到的金额（到分） |
| `extra_cent` | 是否凭余数获得补发的 1 分 |
| `remainder_rank` | 补发排名（从 1 开始；未补发为 `null`） |

另有 `gross_formula` / `base_formula` / `tax_formula` 等可读算式，以及顶层 `checks`
给出全额校验结果（折扣分摊平衡、逐行税区平衡、合计恒等、预期总额比对）。

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

服务地址：

- API 根地址：<http://localhost:8000>
- Swagger UI（交互式文档/调试）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/healthz>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

SQLite 数据文件持久化在 Docker 命名卷 `invoice-data`（容器内 `/data/invoices.db`）。

停止：`docker compose down`（保留数据）；连数据一起清除：`docker compose down -v`。

## 本地直接运行（可选）

```bash
pip install -r requirements.txt
INVOICE_DB_PATH=./invoices.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/invoices` | 创建发票：校验 + 分摊 + 落库，返回计算依据与校验结果（201） |
| `GET` | `/api/v1/invoices/{id}` | 按存储 id 查询发票结果 |
| `GET` | `/api/v1/invoices/by-no/{invoice_no}` | 按发票号查询 |
| `GET` | `/healthz` | 健康检查 |

错误统一形如 `{"error": {"code": "...", "message": "...", "details": {...}}}`，
业务校验失败 `422`、发票号重复 `409`、不存在 `404`。

> 金额字段（`quantity`/`unit_price`/`discount_rate`/`proportion`/`tax_rate`/
> `expected_totals.*`）建议以**字符串**传递（如 `"0.10"`），也接受 JSON 数字；
> 服务端经 `str()` 往返后转 `Decimal`，杜绝二进制浮点误差。响应中所有金额均为字符串。

### 示例：创建发票

```bash
curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "invoice_no": "INV-20260922-001",
    "currency": "CNY",
    "lines": [
      {
        "description": "咨询服务",
        "quantity": "1",
        "unit_price": "100.00",
        "discount_rate": "0.10",
        "tax_zones": [
          {"zone_code": "CN-SH", "proportion": "0.7",  "tax_rate": "0.13"},
          {"zone_code": "CN-HZ", "proportion": "0.3",  "tax_rate": "0.06"}
        ]
      },
      {
        "description": "实施服务",
        "quantity": "2",
        "unit_price": "50.00",
        "discount_rate": "0.10",
        "tax_zones": [
          {"zone_code": "CN-SH", "proportion": "0.5",  "tax_rate": "0.13"},
          {"zone_code": "CN-HZ", "proportion": "0.5",  "tax_rate": "0.06"}
        ]
      }
    ],
    "expected_totals": {
      "gross_total":    "200.00",
      "discount_total": "20.00",
      "net_total":      "180.00",
      "tax_total":      "18.36",
      "payable_total":  "198.36"
    }
  }'
```

结果要点（每行/每税区均带完整分摊依据）：

```jsonc
{
  "id": "……",
  "invoice_no": "INV-20260922-001",
  "result": {
    "totals": {
      "gross_total": "200.00",
      "exact_discount_total": "20.0000",
      "discount_total": "20.00",
      "net_total": "180.00",
      "tax_total": "18.36",
      "payable_total": "198.36"
    },
    "zone_totals": [
      {"zone_code": "CN-HZ", "net_base": "72.00",  "tax": "4.32"},
      {"zone_code": "CN-SH", "net_base": "108.00", "tax": "14.04"}
    ],
    "checks": {
      "discount_allocation_balanced": true,
      "zone_allocations_balanced": [{"line_no": 1, "balanced": true}, {"line_no": 2, "balanced": true}],
      "totals_consistent": true,
      "identities": ["Σ行折扣 = 折扣总额", "每行 Σ税区净基 = 行净额", "毛额 = 折扣 + 净额", "价税合计 = 净额 + 税额"],
      "expected_totals": {"provided": true, "matches": true, "fields": { /* 逐字段 expected/actual */ }}
    }
    // lines[].tax_zones[].allocation 含 exact_share / floor / remainder / extra_cent / remainder_rank
  }
}
```

### 最大余数法示例（1 分钱的稳定归属）

行净额 `10.00`，税区比例 `0.3334 / 0.3334 / 0.3332`（严格求和为 1）：
应摊份额为 `3.334 / 3.334 / 3.332` 分档 floor 为 `3.33 / 3.33 / 3.33`，
还剩 1 分；A、B 余数并列最大（0.4 分），按输入顺序 **A 获得补发**：

```text
A: 3.34 (extra_cent=true,  remainder_rank=1)
B: 3.33 (extra_cent=false)
C: 3.33 (extra_cent=false)
```

同样输入顺序永远得到同样结果——分摊稳定、可审计。

### 被拒绝的两类典型请求

比例之和不为 1：

```jsonc
// HTTP 422
{"error": {"code": "PROPORTION_SUM_MISMATCH",
           "message": "明细行 1 的税区比例之和必须严格等于 1，实际为 1.1",
           "details": {"line_no": 1, "sum": "1.1", "expected": "1"}}}
```

提交总额与重算不符：

```jsonc
// HTTP 422
{"error": {"code": "EXPECTED_TOTAL_MISMATCH",
           "message": "提交的预期总额与按明细重算的总额不一致，发票已拒绝",
           "details": {"mismatches": {"payable_total": {"expected": "12.00", "actual": "11.00"}}}}}
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

覆盖：最大余数法（含平手 tie-break、零权重、确定性）、逐行/税区平衡恒等式、
比例与预期总额拒绝、金额超分拒绝、创建-查询往返、发票号冲突、JSON 金额字符串化等。

## 项目结构

```text
app/
  main.py      # FastAPI 路由、异常处理、生命周期
  calc.py      # Decimal 分摊引擎：最大余数法 + 全部业务校验
  schemas.py   # Pydantic v2 请求/响应模型（Decimal <-> 字符串）
  db.py        # SQLite 存储（结果 JSON 落库）
tests/
  test_allocation.py
Dockerfile
docker-compose.yml
requirements.txt
```

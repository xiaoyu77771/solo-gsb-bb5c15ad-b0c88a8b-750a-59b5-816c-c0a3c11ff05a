"""Decimal 精确分摊引擎。

规则（税外发票 / tax-exclusive）：
  行毛额 gross      = quantity * unit_price（必须精确到分）
  行折扣权重        = gross * discount_rate
  发票折扣总额      = Σ行折扣权重，ROUND_HALF_UP 到分
  行折扣分摊        = 最大余数法（Largest Remainder / Hare）按行权重分配到分
  行净额 net        = gross - 行折扣（到分）
  税区净基          = 行内按 tax zone proportion（Σ必须严格等于 1）最大余数法分摊到分
  税区税额          = net_base * tax_rate，ROUND_HALF_UP 到分

最大余数法：每份额 = 总额(分) * 权重 / 权重和，先各取 floor；剩余的整分按
余数从大到小补发，余数相同则按输入顺序（稳定、可复现）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


class CalcError(ValueError):
    """计算/校验失败，携带错误码与 HTTP 状态，由 API 层统一转换。"""

    def __init__(self, code: str, message: str, http_status: int = 422, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def _d(value: Any) -> Decimal:
    """JSON number/float 经 str() 往返，避免二进制浮点误差。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    """ROUND_HALF_UP 量化到分。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def to_cents(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


@dataclass(frozen=True)
class Share:
    index: int
    raw: Decimal          # 未舍入的应摊份额（单位：分，高精度）
    floor: int             # 向下取整（分）
    remainder: Decimal     # 余数部分 raw - floor（0~1 分）
    allocated_cents: int
    extra_cent: bool       # 是否凭余数获得补发的 1 分
    remainder_rank: int | None  # 获得补发者的余数排名（从 1 开始）

    @property
    def exact_amount(self) -> Decimal:
        return self.raw / 100

    @property
    def allocated(self) -> Decimal:
        return Decimal(self.allocated_cents) / 100


def largest_remainder(weights: list[Decimal], total_cents: int) -> list[Share]:
    """最大余数法把 total_cents（整数分）按非负权重稳定分配。

    返回与 weights 等长、同序的 Share 列表；Σ allocated_cents == total_cents。
    """
    n = len(weights)
    weight_sum = sum(weights, ZERO)
    if weight_sum == 0:
        if total_cents == 0:
            return [
                Share(i, ZERO, 0, ZERO, 0, False, None) for i in range(n)
            ]
        raise CalcError(
            "INCONSISTENT_ALLOCATION",
            "分摊总金额大于 0，但所有权重之和为 0，无法进行最大余数法分摊",
        )

    total = Decimal(total_cents)
    with localcontext() as ctx:
        ctx.prec = 60
        raw_parts = [total * w / weight_sum for w in weights]

    floors = [int(r) for r in raw_parts]  # 份额非负，向 0 截断即 floor
    remainders = [r - f for r, f in zip(raw_parts, floors)]
    leftover = total_cents - sum(floors)
    if not 0 <= leftover <= n:
        # 理论不可达：防御浮点/精度异常导致的不自洽
        raise CalcError(
            "INCONSISTENT_ALLOCATION",
            f"最大余数法舍入差额异常：{leftover}",
        )

    # 余数降序，平手按输入下标升序——稳定 tie-break
    order = sorted(range(n), key=lambda i: (-remainders[i], i))
    awarded = set(order[:leftover])
    rank_of = {idx: k + 1 for k, idx in enumerate(order[:leftover])}

    shares: list[Share] = []
    for i in range(n):
        cents = floors[i] + (1 if i in awarded else 0)
        shares.append(
            Share(
                index=i,
                raw=raw_parts[i],
                floor=floors[i],
                remainder=remainders[i],
                allocated_cents=cents,
                extra_cent=i in awarded,
                remainder_rank=rank_of.get(i),
            )
        )
    return shares


def _share_dict(share: Share) -> dict:
    return {
        "exact_share": share.exact_amount,
        "floor": Decimal(share.floor) / 100,
        "remainder": share.remainder,
        "allocated": share.allocated,
        "extra_cent": share.extra_cent,
        "remainder_rank": share.remainder_rank,
    }


def _validate_zone(line_no: Any, zones: list[dict]) -> list[tuple[str, Decimal, Decimal]]:
    if not zones:
        raise CalcError(
            "EMPTY_TAX_ZONES",
            f"明细行 {line_no} 至少需要一个税区",
        )
    parsed: list[tuple[str, Decimal, Decimal]] = []
    seen: set[str] = set()
    proportion_sum = ZERO
    for z in zones:
        code = str(z["zone_code"]).strip()
        if not code:
            raise CalcError("EMPTY_ZONE_CODE", f"明细行 {line_no} 存在空税区代码")
        if code in seen:
            raise CalcError(
                "DUPLICATE_ZONE",
                f"明细行 {line_no} 中税区 {code} 重复",
                details={"line_no": line_no, "zone_code": code},
            )
        seen.add(code)
        proportion = _d(z["proportion"])
        tax_rate = _d(z["tax_rate"])
        if proportion < 0 or tax_rate < 0:
            raise CalcError("NEGATIVE_VALUE", "税区比例与税率不可为负")
        parsed.append((code, proportion, tax_rate))
        proportion_sum += proportion

    if proportion_sum != ONE:
        raise CalcError(
            "PROPORTION_SUM_MISMATCH",
            f"明细行 {line_no} 的税区比例之和必须严格等于 1，实际为 {proportion_sum}",
            details={
                "line_no": line_no,
                "sum": proportion_sum,
                "expected": "1",
            },
        )
    return parsed


_EXPECTED_FIELDS = (
    "gross_total",
    "discount_total",
    "net_total",
    "tax_total",
    "payable_total",
)


def allocate_invoice(payload: dict[str, Any]) -> dict[str, Any]:
    """根据发票输入执行全部计算与校验，返回可序列化（Decimal）的结果 dict。"""
    raw_lines = payload["lines"]
    if not raw_lines:
        raise CalcError("EMPTY_LINES", "发票至少需要一条明细行")

    # ---- 1. 行毛额与折扣权重 ----
    prepared: list[dict[str, Any]] = []
    for idx, ln in enumerate(raw_lines):
        line_no = ln.get("line_no") or idx + 1
        qty = _d(ln["quantity"])
        unit_price = _d(ln["unit_price"])
        discount_rate = _d(ln["discount_rate"])
        if qty < 0 or unit_price < 0:
            raise CalcError("NEGATIVE_VALUE", f"明细行 {line_no} 数量与单价不可为负")
        if not 0 <= discount_rate <= 1:
            raise CalcError(
                "RATE_OUT_OF_RANGE",
                f"明细行 {line_no} 折扣率必须在 [0, 1] 区间",
            )
        gross_exact = qty * unit_price
        if gross_exact.as_tuple().exponent < -2:
            raise CalcError(
                "AMOUNT_NOT_CENTS",
                f"明细行 {line_no} 毛额 {gross_exact} 超过分（两位小数），"
                "请调整数量或单价使其乘积精确到分",
                details={"line_no": line_no, "gross": gross_exact},
            )
        gross = money(gross_exact)
        prepared.append(
            {
                "line_no": line_no,
                "description": ln.get("description"),
                "quantity": qty,
                "unit_price": unit_price,
                "discount_rate": discount_rate,
                "gross": gross,
                "discount_weight": gross * discount_rate,
                "zone_input": _validate_zone(line_no, ln["tax_zones"]),
            }
        )

    # ---- 2. 发票折扣总额 + 最大余数法分摊到行 ----
    exact_discount_total = sum((p["discount_weight"] for p in prepared), ZERO)
    discount_total = money(exact_discount_total)
    discount_shares = largest_remainder(
        [p["discount_weight"] for p in prepared], to_cents(discount_total)
    )

    lines_out: list[dict[str, Any]] = []
    zone_balances: list[dict[str, Any]] = []
    total_tax = ZERO
    net_total = ZERO
    zone_agg: dict[str, dict[str, Decimal]] = {}

    for p, dshare in zip(prepared, discount_shares):
        line_no = p["line_no"]
        gross = p["gross"]
        line_discount = dshare.allocated
        net = money(gross - line_discount)

        # 行内恒等检查
        if money(gross) != money(line_discount + net):
            raise CalcError(
                "INTERNAL_INCONSISTENCY",
                f"明细行 {line_no} 毛额 ≠ 折扣 + 净额",
                http_status=500,
            )

        net_cents = to_cents(net)
        proportions = [z[1] for z in p["zone_input"]]
        base_shares = largest_remainder(proportions, net_cents)

        zone_out: list[dict[str, Any]] = []
        base_cents_sum = 0
        tax_cents_sum = 0
        for (code, proportion, tax_rate), bshare in zip(p["zone_input"], base_shares):
            base_cents_sum += bshare.allocated_cents
            base = bshare.allocated
            exact_tax = base * tax_rate
            tax = money(exact_tax)
            tax_cents_sum += to_cents(tax)

            agg = zone_agg.setdefault(code, {"net_base": ZERO, "tax": ZERO})
            agg["net_base"] += base
            agg["tax"] += tax

            zone_out.append(
                {
                    "zone_code": code,
                    "proportion": proportion,
                    "allocation": {
                        k: v
                        for k, v in _share_dict(bshare).items()
                        if k != "allocated"
                    }
                    | {"allocated_base": base},
                    "tax_rate": tax_rate,
                    "exact_tax": exact_tax,
                    "tax": tax,
                    "base_formula": (
                        f"{net} × {proportion} = {bshare.exact_amount}"
                        f"（floor {Decimal(bshare.floor) / 100}，余数 {bshare.remainder}）"
                    ),
                    "tax_formula": f"{base} × {tax_rate} = {exact_tax} → {tax}",
                }
            )

        balanced = base_cents_sum == net_cents
        zone_balances.append({"line_no": line_no, "balanced": balanced})
        if not balanced:
            raise CalcError(
                "INTERNAL_INCONSISTENCY",
                f"明细行 {line_no} 各税区净基之和 ≠ 行净额",
                http_status=500,
            )

        total_tax += Decimal(tax_cents_sum) / 100
        net_total += net

        lines_out.append(
            {
                "line_no": line_no,
                "description": p["description"],
                "quantity": p["quantity"],
                "unit_price": p["unit_price"],
                "gross": gross,
                "gross_formula": f"{p['quantity']} × {p['unit_price']} = {gross}",
                "discount_rate": p["discount_rate"],
                "discount": {
                    **_share_dict(dshare),
                    "exact_line_discount": p["discount_weight"],
                    "formula": (
                        f"{gross} × {p['discount_rate']} = {p['discount_weight']}"
                        f"（发票折扣总额 {exact_discount_total} → {discount_total} "
                        f"按权重最大余数法分摊）"
                    ),
                },
                "net": net,
                "net_formula": f"{gross} − {line_discount} = {net}",
                "tax_zones": zone_out,
            }
        )

    # ---- 3. 发票级合计与恒等校验 ----
    gross_total = sum((p["gross"] for p in prepared), ZERO)
    discount_allocated_total = sum(
        (Decimal(s.allocated_cents) / 100 for s in discount_shares), ZERO
    )
    discount_balanced = to_cents(discount_allocated_total) == to_cents(discount_total)
    if not discount_balanced:
        raise CalcError(
            "INTERNAL_INCONSISTENCY", "各行折扣分摊之和 ≠ 折扣总额", http_status=500
        )

    payable_total = money(net_total + total_tax)
    totals_consistent = (
        money(gross_total - discount_total) == money(net_total)
        and payable_total == money(net_total + total_tax)
    )
    if not totals_consistent:
        raise CalcError(
            "INTERNAL_INCONSISTENCY", "发票合计不满足恒等校验", http_status=500
        )

    totals = {
        "gross_total": gross_total,
        "exact_discount_total": exact_discount_total,
        "discount_total": discount_total,
        "net_total": money(net_total),
        "tax_total": money(total_tax),
        "payable_total": payable_total,
    }

    # ---- 4. 调用方提供的预期总额（可选）：不一致直接拒绝 ----
    expected_in = payload.get("expected_totals") or {}
    expected_check: dict[str, Any] = {
        "provided": bool(expected_in),
        "matches": None,
        "fields": {},
    }
    if expected_in:
        mismatches: dict[str, Any] = {}
        for field in _EXPECTED_FIELDS:
            if field not in expected_in or expected_in[field] is None:
                continue
            expected_val = money(_d(expected_in[field]))
            actual_val = totals[field]
            ok = expected_val == actual_val
            expected_check["fields"][field] = {
                "expected": expected_val,
                "actual": actual_val,
                "matches": ok,
            }
            if not ok:
                mismatches[field] = {"expected": expected_val, "actual": actual_val}
        expected_check["matches"] = not mismatches
        if mismatches:
            raise CalcError(
                "EXPECTED_TOTAL_MISMATCH",
                "提交的预期总额与按明细重算的总额不一致，发票已拒绝",
                details={"mismatches": mismatches},
            )

    return {
        "invoice_no": payload["invoice_no"],
        "currency": payload.get("currency") or "CNY",
        "tax_model": "tax_exclusive",
        "rounding": "ROUND_HALF_UP",
        "allocation_method": "largest_remainder_hare",
        "tie_breaker": "input_order",
        "totals": totals,
        "lines": lines_out,
        "zone_totals": [
            {
                "zone_code": code,
                "net_base": money(zone_agg[code]["net_base"]),
                "tax": money(zone_agg[code]["tax"]),
            }
            for code in sorted(zone_agg)
        ],
        "checks": {
            "discount_allocation_balanced": discount_balanced,
            "zone_allocations_balanced": zone_balances,
            "totals_consistent": totals_consistent,
            "identities": [
                "Σ行折扣 = 折扣总额",
                "每行 Σ税区净基 = 行净额",
                "毛额 = 折扣 + 净额",
                "价税合计 = 净额 + 税额",
            ],
            "expected_totals": expected_check,
        },
    }

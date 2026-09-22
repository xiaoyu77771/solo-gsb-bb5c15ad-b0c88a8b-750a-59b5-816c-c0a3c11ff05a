"""请求 / 响应模型。

所有金额以 Decimal 承载并序列化为字符串（JSON 中无浮点，不丢精度）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
)
from pydantic.functional_validators import BeforeValidator
from typing_extensions import Annotated

CENT = Decimal("0.01")


def _as_decimal(value) -> Decimal:
    """float 也先过 str()，切断二进制浮点误差。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# 输入金额：接受 number / string，转 Decimal；量化到分时再由业务规则校验。
Amount = Annotated[Decimal, BeforeValidator(_as_decimal)]
# 输出金额：统一字符串（保留完整精度，如 exact_share 的 0.0000000000000001）
MoneyOut = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"))]


# ---------------- 请求 ----------------

class TaxZoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_code: Annotated[str, Field(min_length=1, max_length=32)]
    proportion: Annotated[Amount, Field(ge=0)]
    tax_rate: Annotated[Amount, Field(ge=0)]


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: Optional[int] = Field(default=None, ge=1)
    description: Optional[Annotated[str, Field(max_length=256)]] = None
    quantity: Annotated[Amount, Field(ge=0)]
    unit_price: Annotated[Amount, Field(ge=0)]
    discount_rate: Annotated[Amount, Field(default=Decimal("0"), ge=0, le=1)]
    tax_zones: list[TaxZoneIn] = Field(min_length=1)


class ExpectedTotalsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gross_total: Optional[Amount] = Field(default=None, ge=0)
    discount_total: Optional[Amount] = Field(default=None, ge=0)
    net_total: Optional[Amount] = Field(default=None, ge=0)
    tax_total: Optional[Amount] = Field(default=None, ge=0)
    payable_total: Optional[Amount] = Field(default=None, ge=0)


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_no: Annotated[str, Field(min_length=1, max_length=64, examples=["INV-20260922-001"])]
    currency: Annotated[str, Field(default="CNY", min_length=3, max_length=8)]
    lines: list[LineIn] = Field(min_length=1)
    expected_totals: Optional[ExpectedTotalsIn] = None


# ---------------- 响应 ----------------

class AllocationDetail(BaseModel):
    exact_share: MoneyOut
    floor: MoneyOut
    remainder: MoneyOut
    allocated: MoneyOut
    extra_cent: bool
    remainder_rank: Optional[int] = None


class DiscountDetail(BaseModel):
    exact_share: MoneyOut
    floor: MoneyOut
    remainder: MoneyOut
    allocated: MoneyOut
    extra_cent: bool
    remainder_rank: Optional[int] = None
    exact_line_discount: MoneyOut
    formula: str


class ZoneBaseOut(BaseModel):
    exact_share: MoneyOut
    floor: MoneyOut
    remainder: MoneyOut
    allocated_base: MoneyOut
    extra_cent: bool
    remainder_rank: Optional[int] = None


class ZoneResultOut(BaseModel):
    zone_code: str
    proportion: MoneyOut
    allocation: ZoneBaseOut
    tax_rate: MoneyOut
    exact_tax: MoneyOut
    tax: MoneyOut
    base_formula: str
    tax_formula: str


class LineOut(BaseModel):
    line_no: int
    description: Optional[str] = None
    quantity: MoneyOut
    unit_price: MoneyOut
    gross: MoneyOut
    gross_formula: str
    discount_rate: MoneyOut
    discount: DiscountDetail
    net: MoneyOut
    net_formula: str
    tax_zones: list[ZoneResultOut]


class TotalsOut(BaseModel):
    gross_total: MoneyOut
    exact_discount_total: MoneyOut
    discount_total: MoneyOut
    net_total: MoneyOut
    tax_total: MoneyOut
    payable_total: MoneyOut


class ZoneTotalOut(BaseModel):
    zone_code: str
    net_base: MoneyOut
    tax: MoneyOut


class FieldCheck(BaseModel):
    expected: MoneyOut
    actual: MoneyOut
    matches: bool


class ExpectedTotalsCheck(BaseModel):
    provided: bool
    matches: Optional[bool] = None
    fields: dict[str, FieldCheck] = Field(default_factory=dict)


class ChecksOut(BaseModel):
    discount_allocation_balanced: bool
    zone_allocations_balanced: list[dict]
    totals_consistent: bool
    identities: list[str]
    expected_totals: ExpectedTotalsCheck


class InvoiceResult(BaseModel):
    invoice_no: str
    currency: str
    tax_model: str
    rounding: str
    allocation_method: str
    tie_breaker: str
    totals: TotalsOut
    lines: list[LineOut]
    zone_totals: list[ZoneTotalOut]
    checks: ChecksOut


class InvoiceRecord(BaseModel):
    """GET 返回：结果 + 元数据。"""

    id: str
    invoice_no: str
    created_at: str
    result: InvoiceResult


class ErrorOut(BaseModel):
    error: dict

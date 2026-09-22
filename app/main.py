"""多税区发票金额分摊 API（FastAPI + SQLite）。

  POST /api/v1/invoices          创建发票（Decimal 精确计算 + 最大余数法分摊 + 校验）
  GET  /api/v1/invoices/{id}     按存储 id 查询结果
  GET  /api/v1/invoices/by-no/{invoice_no}  按发票号查询结果
  GET  /healthz                  健康检查
  GET  /docs                     Swagger UI
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import db
from .calc import CalcError, allocate_invoice
from .schemas import InvoiceIn, InvoiceRecord, InvoiceResult


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="多税区发票金额分摊 API",
    description=(
        "按 Decimal 精确计算折扣与各税区税额，最大余数法（Largest Remainder）"
        "把舍入差额稳定分配到明细行；税区比例之和不为 1 或提交总额与重算不一致时拒绝。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(CalcError)
async def calc_error_handler(request: Request, exc: CalcError) -> Response:
    body = json.dumps(
        {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
        default=lambda o: format(o, "f") if isinstance(o, Decimal) else str(o),
        ensure_ascii=False,
    ).encode("utf-8")
    return Response(content=body, status_code=exc.http_status, media_type="application/json")


@app.post(
    "/api/v1/invoices",
    response_model=InvoiceRecord,
    status_code=201,
    summary="创建发票：校验、分摊并持久化",
    responses={
        422: {"description": "比例/总额不一致等业务校验失败"},
        409: {"description": "发票号已存在"},
    },
)
def create_invoice(invoice: InvoiceIn) -> InvoiceRecord:
    result = allocate_invoice(invoice.model_dump(mode="python"))
    if db.get_invoice(invoice_no=result["invoice_no"]) is not None:
        raise CalcError(
            "DUPLICATE_INVOICE_NO",
            f"发票号 {result['invoice_no']} 已存在",
            http_status=409,
        )
    record = db.insert_invoice(result)
    return InvoiceRecord.model_validate(record)


@app.get(
    "/api/v1/invoices/{invoice_id}",
    response_model=InvoiceRecord,
    summary="按存储 id 查询发票结果",
)
def get_invoice(invoice_id: str) -> InvoiceRecord:
    record = db.get_invoice(invoice_id=invoice_id)
    if record is None:
        raise CalcError("NOT_FOUND", f"发票 {invoice_id} 不存在", http_status=404)
    return InvoiceRecord.model_validate(record)


@app.get(
    "/api/v1/invoices/by-no/{invoice_no}",
    response_model=InvoiceRecord,
    summary="按发票号查询发票结果",
)
def get_invoice_by_no(invoice_no: str) -> InvoiceRecord:
    record = db.get_invoice(invoice_no=invoice_no)
    if record is None:
        raise CalcError("NOT_FOUND", f"发票号 {invoice_no} 不存在", http_status=404)
    return InvoiceRecord.model_validate(record)


@app.get("/healthz", summary="健康检查")
def healthz() -> dict:
    return {"status": "ok"}

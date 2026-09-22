"""FastAPI application: multi-region invoice allocation API."""

from __future__ import annotations

import os
import sqlite3
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .db import InvoiceStore
from .schemas import InvoiceCreate, InvoiceResult, InvoiceSummary
from .service import TotalsMismatchError, compute_invoice

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", "data/invoices.db")

app = FastAPI(
    title="Multi-Region Invoice Allocation API",
    version="1.0.0",
    description=(
        "Computes exact (Decimal) invoice discounts and per-tax-region amounts, "
        "distributes rounding residuals with the largest remainder method, and "
        "rejects inconsistent proportions or totals."
    ),
)

_store: InvoiceStore | None = None


def get_store() -> InvoiceStore:
    global _store
    if _store is None:
        _store = InvoiceStore(DEFAULT_DB_PATH)
    return _store


def to_jsonable(obj: Any) -> Any:
    """Convert Decimals to JSON numbers; amounts are already cent-quantized."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj


def result_response(result: InvoiceResult, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=to_jsonable(result.model_dump())
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "multi-region-invoice-allocation",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "create_invoice": "POST /api/v1/invoices",
            "get_invoice": "GET /api/v1/invoices/{invoice_id}",
            "list_invoices": "GET /api/v1/invoices",
        },
    }


@app.post(
    "/api/v1/invoices",
    status_code=201,
    response_model=InvoiceResult,
    tags=["invoices"],
    responses={
        400: {"description": "Expected totals do not match computed totals"},
        409: {"description": "Invoice number already exists"},
        422: {"description": "Invalid input (e.g. proportions do not sum to 1)"},
    },
)
def create_invoice(
    payload: InvoiceCreate, store: InvoiceStore = Depends(get_store)
) -> JSONResponse:
    try:
        result = compute_invoice(payload)
    except TotalsMismatchError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "totals_mismatch",
                "message": "expected totals do not match computed totals",
                "mismatches": exc.mismatches,
            },
        )

    try:
        store.insert(
            invoice_id=result.invoice_id,
            invoice_number=result.invoice_number,
            request=payload.model_dump(),
            result=result.model_dump(),
            created_at=result.created_at,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"invoice_number '{payload.invoice_number}' already exists",
        )

    return result_response(result, status_code=201)


@app.get(
    "/api/v1/invoices/{invoice_id}",
    response_model=InvoiceResult,
    tags=["invoices"],
    responses={404: {"description": "Invoice not found"}},
)
def get_invoice(
    invoice_id: str, store: InvoiceStore = Depends(get_store)
) -> JSONResponse:
    stored = store.get_result(invoice_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    # Re-validate so Decimal fields round-trip exactly, then render as numbers.
    result = InvoiceResult.model_validate(stored)
    return result_response(result)


@app.get("/api/v1/invoices", response_model=list[InvoiceSummary], tags=["invoices"])
def list_invoices(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    store: InvoiceStore = Depends(get_store),
) -> JSONResponse:
    return JSONResponse(content=to_jsonable(store.list_summaries(limit, offset)))

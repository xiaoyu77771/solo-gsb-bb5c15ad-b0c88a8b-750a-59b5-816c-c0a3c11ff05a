"""计算引擎与 API 端到端测试。"""

import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.calc import CalcError, allocate_invoice, largest_remainder
from app.main import app
from app import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db._conn = None
    db.init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---------------- 最大余数法 ----------------

def test_largest_remainder_classic_split():
    # 10 分按 1:1:1 分：3.333... -> 4,3,3，第 1 行补发（平手按下标）
    shares = largest_remainder([Decimal(1)] * 3, 10)
    assert [s.allocated_cents for s in shares] == [4, 3, 3]
    assert shares[0].extra_cent and shares[0].remainder_rank == 1
    assert not shares[1].extra_cent and shares[1].remainder_rank is None


def test_largest_remainder_deterministic_order():
    # 100 分按 3:7 分两次，顺序无关、稳定
    a = largest_remainder([Decimal(3), Decimal(7)], 100)
    b = largest_remainder([Decimal(7), Decimal(3)], 100)
    assert [s.allocated_cents for s in a] == [30, 70]
    assert [s.allocated_cents for s in b] == [70, 30]
    assert sum(s.allocated_cents for s in a) == 100


def test_largest_remainder_zero_weights_with_amount_rejected():
    with pytest.raises(CalcError):
        largest_remainder([Decimal(0), Decimal(0)], 5)


def test_largest_remainder_zero_total():
    shares = largest_remainder([Decimal(1), Decimal(2)], 0)
    assert [s.allocated_cents for s in shares] == [0, 0]


# ---------------- 整票计算 ----------------

def _zone(code, proportion, rate):
    return {"zone_code": code, "proportion": proportion, "tax_rate": rate}


def _sample_payload(expected=True):
    payload = {
        "invoice_no": "INV-001",
        "currency": "CNY",
        "lines": [
            {
                "description": "A",
                "quantity": "1",
                "unit_price": "100.00",
                "discount_rate": "0.10",
                "tax_zones": [_zone("Z1", "0.7", "0.13"), _zone("Z2", "0.3", "0.06")],
            },
            {
                "description": "B",
                "quantity": "2",
                "unit_price": "50.00",
                "discount_rate": "0.10",
                "tax_zones": [_zone("Z1", "0.5", "0.13"), _zone("Z2", "0.5", "0.06")],
            },
        ],
    }
    if expected:
        payload["expected_totals"] = {
            "gross_total": "200.00",
            "discount_total": "20.00",
            "net_total": "180.00",
            "tax_total": "18.36",
            "payable_total": "198.36",
        }
    return payload


def test_sample_invoice_allocation():
    r = allocate_invoice(_sample_payload())
    t = r["totals"]
    assert t["gross_total"] == Decimal("200.00")
    assert t["discount_total"] == Decimal("20.00")
    assert t["net_total"] == Decimal("180.00")
    assert t["tax_total"] == Decimal("18.36")
    assert t["payable_total"] == Decimal("198.36")

    # 折扣：两行各 10 分权重、总 2000 分 -> 10.00 / 10.00
    assert [ln["discount"]["allocated"] for ln in r["lines"]] == [
        Decimal("10.00"),
        Decimal("10.00"),
    ]

    # 第二行 net=90 按 50/50 分：45.00 / 45.00
    l2_zones = {z["zone_code"]: z for z in r["lines"][1]["tax_zones"]}
    assert l2_zones["Z1"]["allocation"]["allocated_base"] == Decimal("45.00")
    assert l2_zones["Z2"]["allocation"]["allocated_base"] == Decimal("45.00")
    # Z1 税 45×13%=5.85, Z2 税 45×6%=2.70
    assert l2_zones["Z1"]["tax"] == Decimal("5.85")
    assert l2_zones["Z2"]["tax"] == Decimal("2.70")

    # 第一行 net=90 按 70/30：63 / 27
    l1 = {z["zone_code"]: z for z in r["lines"][0]["tax_zones"]}
    assert l1["Z1"]["allocation"]["allocated_base"] == Decimal("63.00")
    assert l1["Z2"]["allocation"]["allocated_base"] == Decimal("27.00")
    assert l1["Z1"]["tax"] == Decimal("8.19")
    assert l1["Z2"]["tax"] == Decimal("1.62")

    # 每一行的税区净基之和必须精确等于行净额
    for ln in r["lines"]:
        s = sum((z["allocation"]["allocated_base"] for z in ln["tax_zones"]), Decimal(0))
        assert s == ln["net"]

    assert r["checks"]["discount_allocation_balanced"]
    assert all(c["balanced"] for c in r["checks"]["zone_allocations_balanced"])
    assert r["checks"]["totals_consistent"]
    assert r["checks"]["expected_totals"]["matches"] is True

    ztot = {z["zone_code"]: z for z in r["zone_totals"]}
    assert ztot["Z1"]["net_base"] == Decimal("108.00")
    assert ztot["Z1"]["tax"] == Decimal("14.04")
    assert ztot["Z2"]["net_base"] == Decimal("72.00")
    assert ztot["Z2"]["tax"] == Decimal("4.32")


def test_remainder_cent_tie_breaker():
    # net=10.00；比例 0.3334/0.3334/0.3332（严格求和=1）：
    # raw 333.4 / 333.4 / 333.2 分，A、B 余数并列最大 -> 按输入顺序 A 补发 1 分
    payload = {
        "invoice_no": "X",
        "lines": [
            {
                "quantity": "1",
                "unit_price": "10.00",
                "discount_rate": "0",
                "tax_zones": [
                    _zone("A", "0.3334", "0"),
                    _zone("B", "0.3334", "0"),
                    _zone("C", "0.3332", "0"),
                ],
            }
        ],
    }
    r = allocate_invoice(payload)
    bases = [z["allocation"]["allocated_base"] for z in r["lines"][0]["tax_zones"]]
    assert bases == [Decimal("3.34"), Decimal("3.33"), Decimal("3.33")]
    zones = r["lines"][0]["tax_zones"]
    assert zones[0]["allocation"]["extra_cent"] is True
    assert zones[0]["allocation"]["remainder_rank"] == 1
    assert zones[1]["allocation"]["extra_cent"] is False
    assert zones[1]["allocation"]["remainder_rank"] is None


def test_discount_remainder_allocated_to_lines():
    # 三行毛额各 100，折扣率 33.33% -> 精确折扣 33.33 每行，总 99.99
    payload = {
        "invoice_no": "D",
        "lines": [
            {
                "quantity": "1",
                "unit_price": "100.00",
                "discount_rate": "0.3333",
                "tax_zones": [_zone("Z1", "1", "0")],
            }
            for _ in range(3)
        ],
    }
    r = allocate_invoice(payload)
    discounts = [ln["discount"]["allocated"] for ln in r["lines"]]
    assert sum(discounts) == r["totals"]["discount_total"]
    assert r["totals"]["discount_total"] == Decimal("99.99")
    # 33.33 各 -> 99.99 精确，无补发
    assert discounts == [Decimal("33.33")] * 3


def test_discount_rounding_remainder():
    # 折扣总额出现小数厘：两行同权重，差额稳定落第 1 行
    payload = {
        "invoice_no": "D2",
        "lines": [
            {
                "quantity": "1",
                "unit_price": "100.01",
                "discount_rate": "0.10",
                "tax_zones": [_zone("Z1", "1", "0")],
            },
            {
                "quantity": "1",
                "unit_price": "100.01",
                "discount_rate": "0.10",
                "tax_zones": [_zone("Z1", "1", "0")],
            },
        ],
    }
    r = allocate_invoice(payload)
    # 每行精确 10.001，总 20.002 -> 20.00；10.00 / 10.00（余数相同，都未超半分）
    assert r["totals"]["discount_total"] == Decimal("20.00")
    assert sum(ln["discount"]["allocated"] for ln in r["lines"]) == Decimal("20.00")


def test_proportion_sum_rejected():
    payload = _sample_payload()
    payload["lines"][0]["tax_zones"][0]["proportion"] = "0.6"
    with pytest.raises(CalcError) as ei:
        allocate_invoice(payload)
    assert ei.value.code == "PROPORTION_SUM_MISMATCH"


def test_duplicate_zone_in_line_rejected():
    payload = _sample_payload()
    payload["lines"][0]["tax_zones"] = [
        _zone("Z1", "0.7", "0"),
        _zone("Z2", "0.3", "0"),
        _zone("Z1", "0", "0"),
    ]
    with pytest.raises(CalcError) as ei:
        allocate_invoice(payload)
    assert ei.value.code == "DUPLICATE_ZONE"


def test_expected_total_mismatch_rejected():
    payload = _sample_payload()
    payload["expected_totals"]["tax_total"] = "99.99"
    with pytest.raises(CalcError) as ei:
        allocate_invoice(payload)
    assert ei.value.code == "EXPECTED_TOTAL_MISMATCH"
    assert "tax_total" in ei.value.details["mismatches"]


def test_gross_over_cents_rejected():
    payload = {
        "invoice_no": "P",
        "lines": [
            {
                "quantity": "0.333",
                "unit_price": "10.00",
                "discount_rate": "0",
                "tax_zones": [_zone("Z1", "1", "0")],
            }
        ],
    }
    with pytest.raises(CalcError) as ei:
        allocate_invoice(payload)
    assert ei.value.code == "AMOUNT_NOT_CENTS"


# ---------------- API 端到端 ----------------

def test_api_create_get_roundtrip(client):
    payload = _sample_payload()
    resp = client.post("/api/v1/invoices", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["result"]["totals"]["payable_total"] == "198.36"  # 字符串，非 float
    inv_id = body["id"]

    # 金额以字符串输出，无浮点污染
    assert isinstance(body["result"]["lines"][0]["gross"], str)

    got = client.get(f"/api/v1/invoices/{inv_id}")
    assert got.status_code == 200
    assert got.json()["id"] == inv_id

    got_no = client.get("/api/v1/invoices/by-no/INV-001")
    assert got_no.status_code == 200
    assert got_no.json()["id"] == inv_id


def test_api_duplicate_invoice_no_conflict(client):
    client.post("/api/v1/invoices", json=_sample_payload())
    resp = client.post("/api/v1/invoices", json=_sample_payload())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_INVOICE_NO"


def test_api_proportion_mismatch_422(client):
    payload = _sample_payload()
    payload["lines"][0]["tax_zones"][0]["proportion"] = "0.8"
    resp = client.post("/api/v1/invoices", json=copy.deepcopy(payload))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROPORTION_SUM_MISMATCH"


def test_api_expected_total_mismatch_422(client):
    payload = _sample_payload()
    payload["expected_totals"]["payable_total"] = "200.00"
    resp = client.post("/api/v1/invoices", json=copy.deepcopy(payload))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "EXPECTED_TOTAL_MISMATCH"


def test_api_schema_validation_float_survives(client):
    # float 输入经 str() 往返，0.1+0.2 类污染不应出现
    payload = {
        "invoice_no": "F",
        "lines": [
            {
                "quantity": 1,
                "unit_price": 0.30,
                "discount_rate": 0,
                "tax_zones": [{"zone_code": "Z1", "proportion": 1, "tax_rate": 0}],
            }
        ],
    }
    resp = client.post("/api/v1/invoices", json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["result"]["totals"]["gross_total"] == "0.30"


def test_api_not_found(client):
    assert client.get("/api/v1/invoices/nope").status_code == 404
    assert client.get("/api/v1/invoices/by-no/nope").status_code == 404


def test_api_empty_lines_rejected(client):
    resp = client.post("/api/v1/invoices", json={"invoice_no": "E", "lines": []})
    assert resp.status_code == 422


def test_api_extra_field_rejected(client):
    payload = _sample_payload()
    payload["bogus"] = 1
    resp = client.post("/api/v1/invoices", json=payload)
    assert resp.status_code == 422


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}

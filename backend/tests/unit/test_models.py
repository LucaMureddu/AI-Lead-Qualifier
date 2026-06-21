"""
tests/unit/test_models.py
-------------------------
Unit test puri per i validator di ``ServiceItem`` e i conteggi di ``ServiceCatalog``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ingestion.models import PriceType, ServiceCatalog, ServiceItem

pytestmark = pytest.mark.unit


def _item(**ovr) -> ServiceItem:
    base = {"tenant_id": "acme", "name": "Servizio Test", "price": 100.0}
    base.update(ovr)
    return ServiceItem(**base)


def test_price_negative_raises() -> None:
    with pytest.raises(ValidationError):
        _item(price=-1.0)


def test_price_rounded_to_4dp() -> None:
    assert _item(price=10.123456).price == 10.1235


def test_currency_uppercased() -> None:
    assert _item(currency="eur").currency == "EUR"


def test_currency_must_be_three_letters() -> None:
    with pytest.raises(ValidationError):
        _item(currency="EU")
    with pytest.raises(ValidationError):
        _item(currency="EURO")


def test_unit_lowercased() -> None:
    assert _item(unit="HOUR").unit == "hour"


def test_unit_none_stays_none() -> None:
    assert _item(unit=None).unit is None


def test_low_confidence_auto_flagged() -> None:
    item = _item(confidence=0.3)
    assert item.flagged is True
    assert item.flag_reason and "0.30" in item.flag_reason


def test_high_confidence_not_flagged() -> None:
    assert _item(confidence=0.9).flagged is False


def test_confidence_out_of_range_raises() -> None:
    with pytest.raises(ValidationError):
        _item(confidence=1.5)


def test_name_required() -> None:
    with pytest.raises(ValidationError):
        ServiceItem(tenant_id="acme", price=1.0)  # type: ignore[call-arg]


def test_tenant_id_required() -> None:
    with pytest.raises(ValidationError):
        ServiceItem(name="X", price=1.0)  # type: ignore[call-arg]


def test_service_catalog_computes_counts() -> None:
    items = [_item(name="A", confidence=0.9), _item(name="B", confidence=0.3)]
    cat = ServiceCatalog(tenant_id="acme", items=items, source_file="f.csv")
    assert cat.total_items == 2
    assert cat.flagged_count == 1  # l'item con confidence 0.3 è auto-flaggato


# ── V3: PriceType, coercizione, is_computable ────────────────────────────────

class TestPriceType:
    """Casi di coercizione dell'invariante ibrido e property is_computable."""

    def test_variable_with_explicit_price_forces_none(self) -> None:
        """price_type=VARIABLE + price=999 → price forzato a None."""
        item = _item(price_type=PriceType.VARIABLE, price=999.0)
        assert item.price is None
        assert item.price_type == PriceType.VARIABLE

    def test_free_with_explicit_price_forces_zero(self) -> None:
        """price_type=FREE + price=999 → price forzato a 0.0."""
        item = _item(price_type=PriceType.FREE, price=999.0)
        assert item.price == 0.0
        assert item.price_type == PriceType.FREE

    def test_fixed_with_none_price_raises(self) -> None:
        """price_type=FIXED + price=None → ValueError (loud failure)."""
        with pytest.raises(ValidationError):
            _item(price_type=PriceType.FIXED, price=None)

    def test_is_computable_fixed(self) -> None:
        assert _item(price_type=PriceType.FIXED, price=100.0).is_computable is True

    def test_is_computable_free(self) -> None:
        assert _item(price_type=PriceType.FREE).is_computable is True

    def test_is_computable_variable(self) -> None:
        assert _item(price_type=PriceType.VARIABLE).is_computable is False

    def test_infer_variable_when_price_is_none(self) -> None:
        """Se price=None e price_type non specificato → inferito VARIABLE."""
        item = ServiceItem(tenant_id="acme", name="X", price=None)
        assert item.price_type == PriceType.VARIABLE
        assert item.price is None

    def test_infer_fixed_when_price_provided(self) -> None:
        """Se price=50 e price_type non specificato → inferito FIXED."""
        item = ServiceItem(tenant_id="acme", name="X", price=50.0)
        assert item.price_type == PriceType.FIXED

    def test_default_price_type_is_fixed(self) -> None:
        """Il default esplicito resta FIXED quando price=0.0 (non None)."""
        item = _item(price=0.0)
        assert item.price_type == PriceType.FIXED

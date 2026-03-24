"""KESCO Energy sensor entities."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import KescoCoordinator

CURRENCY_EUR = "EUR"


@dataclass(frozen=True)
class KescoSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with a value extractor.

    value_fn(data) -> state value  where data = {"invoices": [...], "balance": {...}}
    attr_fn(data)  -> extra_state_attributes dict
    """
    value_fn: Any = None
    attr_fn: Any = None


def _month_label(inv: dict) -> str:
    month_name = calendar.month_abbr[inv.get("BillingPeriodID", 0)]
    year = inv.get("BillingPeriodYear", "")
    return f"{month_name} {year}"


def _invs(data: dict) -> list:
    return data.get("invoices") or []


def _bal(data: dict) -> dict:
    return data.get("balance") or {}


def _safe_get(invoices: list, index: int) -> dict | None:
    return invoices[index] if len(invoices) > index else None


SENSOR_DESCRIPTIONS: tuple[KescoSensorDescription, ...] = (
    # --- Latest invoice ---
    KescoSensorDescription(
        key="latest_bill_amount",
        name="KESCO Latest Bill Amount",
        native_unit_of_measurement=CURRENCY_EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (_safe_get(_invs(d), 0) or {}).get("BillAmount"),
        attr_fn=lambda d: (
            {"reference": _safe_get(_invs(d), 0).get("ReferenceNumber")}
            if _safe_get(_invs(d), 0) else {}
        ),
    ),
    KescoSensorDescription(
        key="latest_bill_kwh",
        name="KESCO Latest Bill kWh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (_safe_get(_invs(d), 0) or {}).get("TotalConsum"),
    ),
    KescoSensorDescription(
        key="latest_bill_a1_kwh",
        name="KESCO Latest Bill A1 kWh (Day)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (_safe_get(_invs(d), 0) or {}).get("ActiveHighConsum"),
    ),
    KescoSensorDescription(
        key="latest_bill_a2_kwh",
        name="KESCO Latest Bill A2 kWh (Night)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (_safe_get(_invs(d), 0) or {}).get("ActiveLowConsum"),
    ),
    KescoSensorDescription(
        key="latest_reading_date",
        name="KESCO Latest Reading Date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: (
            ((_safe_get(_invs(d), 0) or {}).get("ReadingDate") or "")[:10] or None
        ),
    ),
    KescoSensorDescription(
        key="latest_due_date",
        name="KESCO Latest Due Date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: (
            ((_safe_get(_invs(d), 0) or {}).get("DueDate") or "")[:10] or None
        ),
    ),
    KescoSensorDescription(
        key="latest_period",
        name="KESCO Latest Period",
        value_fn=lambda d: (
            _month_label(_safe_get(_invs(d), 0)) if _safe_get(_invs(d), 0) else None
        ),
    ),
    KescoSensorDescription(
        key="latest_reference",
        name="KESCO Latest Reference",
        value_fn=lambda d: (_safe_get(_invs(d), 0) or {}).get("ReferenceNumber"),
    ),
    # --- Previous invoice ---
    KescoSensorDescription(
        key="previous_bill_amount",
        name="KESCO Previous Bill Amount",
        native_unit_of_measurement=CURRENCY_EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (_safe_get(_invs(d), 1) or {}).get("BillAmount"),
    ),
    KescoSensorDescription(
        key="previous_bill_kwh",
        name="KESCO Previous Bill kWh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (_safe_get(_invs(d), 1) or {}).get("TotalConsum"),
    ),
    KescoSensorDescription(
        key="previous_reading_date",
        name="KESCO Previous Reading Date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: (
            ((_safe_get(_invs(d), 1) or {}).get("ReadingDate") or "")[:10] or None
        ),
    ),
    KescoSensorDescription(
        key="previous_period",
        name="KESCO Previous Period",
        value_fn=lambda d: (
            _month_label(_safe_get(_invs(d), 1)) if _safe_get(_invs(d), 1) else None
        ),
    ),
    # --- Totals ---
    KescoSensorDescription(
        key="total_billed_all_time",
        name="KESCO Total Billed All Time",
        native_unit_of_measurement=CURRENCY_EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (
            round(sum(inv.get("BillAmount", 0) for inv in _invs(d)), 2)
            if _invs(d) else None
        ),
    ),
    KescoSensorDescription(
        key="total_kwh_all_time",
        name="KESCO Total kWh All Time",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: (
            round(sum(inv.get("TotalConsum", 0) for inv in _invs(d)), 2)
            if _invs(d) else None
        ),
    ),
    KescoSensorDescription(
        key="invoice_count",
        name="KESCO Invoice Count",
        native_unit_of_measurement="invoices",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: len(_invs(d)),
        attr_fn=lambda d: {"invoices": _invs(d)},
    ),
    # --- Balance / Account ---
    KescoSensorDescription(
        key="current_balance",
        name="KESCO Current Balance",
        native_unit_of_measurement=CURRENCY_EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: _bal(d).get("CurrentBalance"),
        attr_fn=lambda d: {
            "consumer_name": _bal(d).get("ConsumerName"),
            "billing_address": _bal(d).get("BillingAddress"),
            "meter_id": _bal(d).get("AMeterId"),
            "tariff_group": _bal(d).get("TariffGroup"),
            "kesco_balance": _bal(d).get("KescoBalance"),
            "kek_balance": _bal(d).get("KekBalance"),
            "last_bill_amount": _bal(d).get("LastBillAmount"),
            "a1_price": _bal(d).get("A1Price"),
            "a2_price": _bal(d).get("A2Price"),
        } if _bal(d) else {},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: KescoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        KescoSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS
    )


class KescoSensor(CoordinatorEntity[KescoCoordinator], SensorEntity):
    """A single KESCO sensor backed by coordinator data."""

    entity_description: KescoSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: KescoCoordinator,
        description: KescoSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.attr_fn and self.coordinator.data:
            return self.entity_description.attr_fn(self.coordinator.data)
        return None

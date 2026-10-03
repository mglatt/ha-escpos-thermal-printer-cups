"""Sensor platform: when the printer last finished a print job."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .entity import printer_device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:  # type: ignore[no-untyped-def]
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    async_add_entities([EscposLastPrintSensor(entry, adapter)])


class EscposLastPrintSensor(RestoreSensor):
    """Time CUPS last reported a print job from this integration as completed.

    Unlike the Online sensor's last_ok (refreshed by status probes too), this
    only moves when text, QR, image, barcode or sample prints actually
    complete; feed/cut/beep don't count. The value survives restarts so a
    "nothing printed today" automation doesn't misfire after one.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_print"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, adapter: Any) -> None:
        self._entry = entry
        self._adapter = adapter
        self._unsubscribe: Callable[[], None] | None = None
        self._attr_unique_id = f"{entry.entry_id}_last_print"
        self._attr_native_value: datetime | None = adapter.last_print
        self._job_id: int | None = adapter.last_print_job_id

    @property
    def device_info(self) -> DeviceInfo:
        return printer_device_info(self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"job_id": self._job_id}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None and (last := await self.async_get_last_sensor_data()):
            if isinstance(last.native_value, datetime):
                self._attr_native_value = last.native_value
            if (old := await self.async_get_last_state()) is not None:
                self._job_id = old.attributes.get("job_id")

        def _on_print() -> None:
            self._attr_native_value = self._adapter.last_print
            self._job_id = self._adapter.last_print_job_id
            self.async_write_ha_state()

        self._unsubscribe = self._adapter.add_print_listener(_on_print)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            with contextlib.suppress(Exception):
                self._unsubscribe()
            self._unsubscribe = None

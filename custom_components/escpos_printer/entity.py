"""Shared entity helpers for ESC/POS printer platforms."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def printer_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Device registry info shared by every entity of one printer entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"ESC/POS Printer {entry.title}",
        manufacturer="ESC/POS",
        model="CUPS Printer",
    )

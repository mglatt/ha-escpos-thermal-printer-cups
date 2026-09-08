from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CODEPAGE,
    CONF_IMPL,
    CONF_LINE_WIDTH,
    CONF_PRINTER_NAME,
    CONF_PROFILE,
    CONF_STATUS_INTERVAL,
    CONF_WIDTH_PIXELS,
    DOMAIN,
)

TO_REDACT: set[str] = set()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = dict(entry.data)
    options = dict(entry.options)

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    adapter = store.get("adapter")

    runtime: dict[str, Any] = {}
    if adapter is not None:
        # Get config once to avoid repeated nested getattr calls
        config = getattr(adapter, "_config", None)

        try:
            width_pixels = adapter.image_target_width()
        except Exception:
            width_pixels = None

        runtime = {
            "status": adapter.get_status(),
            "diagnostics": adapter.get_diagnostics(),
            "profile": config.profile if config else None,
            "codepage": config.codepage if config else None,
            "line_width": config.line_width if config else None,
            "printer_name": config.printer_name if config else None,
            "status_interval": getattr(adapter, "_status_interval", None),
            "width_pixels": width_pixels,
            "default_impl": config.impl if config else None,
        }

    payload = {
        "entry": {
            "title": entry.title,
            "data": {
                CONF_PRINTER_NAME: data.get(CONF_PRINTER_NAME),
                CONF_CODEPAGE: data.get(CONF_CODEPAGE),
                CONF_PROFILE: data.get(CONF_PROFILE),
                CONF_LINE_WIDTH: data.get(CONF_LINE_WIDTH),
                CONF_WIDTH_PIXELS: data.get(CONF_WIDTH_PIXELS),
                CONF_IMPL: data.get(CONF_IMPL),
            },
            "options": {
                CONF_CODEPAGE: options.get(CONF_CODEPAGE),
                CONF_PROFILE: options.get(CONF_PROFILE),
                CONF_LINE_WIDTH: options.get(CONF_LINE_WIDTH),
                CONF_STATUS_INTERVAL: options.get(CONF_STATUS_INTERVAL),
                CONF_WIDTH_PIXELS: options.get(CONF_WIDTH_PIXELS),
                CONF_IMPL: options.get(CONF_IMPL),
            },
        },
        "runtime": runtime,
    }

    return async_redact_data(payload, TO_REDACT)


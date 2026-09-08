"""Tests for config entry diagnostics."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.escpos_printer.const import (
    CONF_PRINTER_NAME,
    CONF_STATUS_INTERVAL,
    DOMAIN,
)
from custom_components.escpos_printer.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_payload(hass):  # type: ignore[no-untyped-def]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TestPrinter",
        data={CONF_PRINTER_NAME: "TestPrinter"},
        options={CONF_STATUS_INTERVAL: 0},
        unique_id="cups_TestPrinter",
    )
    entry.add_to_hass(hass)
    with patch("escpos.printer.Dummy"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["entry"]["data"][CONF_PRINTER_NAME] == "TestPrinter"
    assert payload["entry"]["options"][CONF_STATUS_INTERVAL] == 0
    runtime = payload["runtime"]
    assert runtime["printer_name"] == "TestPrinter"
    assert "keepalive" not in runtime
    assert "diagnostics" in runtime
    # Effective image settings are surfaced for support requests
    assert runtime["width_pixels"] == 512  # fallback: no override, no profile
    assert runtime["default_impl"] is None


async def test_diagnostics_without_adapter(hass):  # type: ignore[no-untyped-def]
    """Diagnostics must not fail when the entry never finished setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Orphan",
        data={CONF_PRINTER_NAME: "Orphan"},
        unique_id="cups_Orphan",
    )
    entry.add_to_hass(hass)

    payload = await async_get_config_entry_diagnostics(hass, entry)
    assert payload["entry"]["data"][CONF_PRINTER_NAME] == "Orphan"

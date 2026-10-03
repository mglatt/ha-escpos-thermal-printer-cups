"""Tests for the device-page buttons and the sample print."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.escpos_printer.button import FEED_LINES
from custom_components.escpos_printer.const import CONF_DEFAULT_CUT, CONF_PRINTER_NAME, DOMAIN
from custom_components.escpos_printer.printer import (
    SAMPLE_QR_URL,
    EscposPrinterAdapter,
    PrinterConfig,
)

PREFIX = "button.esc_pos_printer_testprinter_"


async def _setup_entry(hass, cut: str | None = None):  # type: ignore[no-untyped-def]
    data: dict[str, Any] = {CONF_PRINTER_NAME: "TestPrinter"}
    if cut is not None:
        data[CONF_DEFAULT_CUT] = cut
    entry = MockConfigEntry(
        domain=DOMAIN, title="TestPrinter", data=data, unique_id="cups_TestPrinter"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]["adapter"]


async def _press(hass, key: str) -> None:  # type: ignore[no-untyped-def]
    await hass.services.async_call(
        "button", "press", {"entity_id": PREFIX + key}, blocking=True
    )


async def test_buttons_are_created_on_the_printer_device(hass):  # type: ignore[no-untyped-def]
    await _setup_entry(hass)
    for key in ("feed_paper", "cut_paper", "beep", "sample_print"):
        assert hass.states.get(PREFIX + key) is not None, key


async def test_feed_button(hass):  # type: ignore[no-untyped-def]
    _entry, adapter = await _setup_entry(hass)
    with patch.object(adapter, "feed", AsyncMock()) as mock_feed:
        await _press(hass, "feed_paper")
    assert mock_feed.call_args.kwargs == {"lines": FEED_LINES}


async def test_beep_button(hass):  # type: ignore[no-untyped-def]
    _entry, adapter = await _setup_entry(hass)
    with patch.object(adapter, "beep", AsyncMock()) as mock_beep:
        await _press(hass, "beep")
    mock_beep.assert_awaited_once()


@pytest.mark.parametrize(("default_cut", "expected"), [(None, "full"), ("none", "full"), ("partial", "partial")])
async def test_cut_button_never_does_nothing(hass, default_cut, expected):  # type: ignore[no-untyped-def]
    _entry, adapter = await _setup_entry(hass, cut=default_cut)
    with patch.object(adapter, "cut", AsyncMock()) as mock_cut:
        await _press(hass, "cut_paper")
    assert mock_cut.call_args.kwargs == {"mode": expected}


async def test_sample_print_button_passes_title_and_cut(hass):  # type: ignore[no-untyped-def]
    _entry, adapter = await _setup_entry(hass, cut="none")
    with patch.object(adapter, "print_sample", AsyncMock()) as mock_sample:
        await _press(hass, "sample_print")
    assert mock_sample.call_args.kwargs == {"title": "TestPrinter", "cut": "full"}


async def test_button_errors_surface_as_homeassistant_error(hass):  # type: ignore[no-untyped-def]
    _entry, adapter = await _setup_entry(hass)
    with (
        patch.object(adapter, "feed", AsyncMock(side_effect=RuntimeError("CUPS down"))),
        pytest.raises(HomeAssistantError, match="CUPS down"),
    ):
        await _press(hass, "feed_paper")


class HassStub:
    async def async_add_executor_job(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


async def test_print_sample_is_one_tracked_job_with_ruler_and_qr() -> None:
    fake = MagicMock()
    fake.profile.get_columns.side_effect = lambda f: {"a": 32, "b": 42}[f]
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P", line_width=32))
    adapter._connect = lambda: fake  # type: ignore[method-assign]
    adapter._submit_job = AsyncMock(return_value=3)  # type: ignore[method-assign]
    with patch.object(adapter, "_start_job_tracking") as mock_track:
        await adapter.print_sample(HassStub(), title="Kitchen", cut="full")

    adapter._submit_job.assert_awaited_once()  # type: ignore[attr-defined]
    mock_track.assert_called_once()
    printed = "".join(c.args[0] for c in fake.text.call_args_list)
    assert "Kitchen" in printed
    assert "Line width: 32 columns\n12345678901234567890123456789012\n" in printed
    fake.qr.assert_called_once_with(SAMPLE_QR_URL, size=4)
    fake.cut.assert_called_once()
    # Baseline style resets invert before anything is printed
    assert fake.set.call_args_list[0].kwargs["invert"] is False


async def test_sample_print_button_end_to_end(hass):  # type: ignore[no-untyped-def]
    """Real build path on the conftest fake printer: bytes reach CUPS once."""
    _entry, adapter = await _setup_entry(hass)
    submitted: list[bytes] = []

    async def _capture(printer: Any) -> int:
        submitted.append(printer.output)
        return 5

    with patch.object(adapter, "_submit_job", _capture):
        await _press(hass, "sample_print")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(submitted) == 1
    assert b"ESC/POS SAMPLE PRINT" in submitted[0]
    assert b"\x1dQR" in submitted[0]
    assert adapter.last_print_job_id == 5

"""Tests for CUPS job tracking and the Last print sensor."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import State
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.escpos_printer import printer as printer_mod
from custom_components.escpos_printer.const import CONF_PRINTER_NAME, DOMAIN
from custom_components.escpos_printer.printer import (
    JOB_STATE_ABORTED,
    JOB_STATE_CANCELED,
    JOB_STATE_COMPLETED,
    EscposPrinterAdapter,
    PrinterConfig,
    get_cups_job_state,
)

SENSOR_ID = "sensor.esc_pos_printer_testprinter_last_print"
PROCESSING = 5


class HassStub:
    async def async_add_executor_job(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _adapter(job_id: int = 7) -> EscposPrinterAdapter:
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P"))
    adapter._connect = MagicMock  # type: ignore[method-assign,assignment]
    adapter._submit_job = AsyncMock(return_value=job_id)  # type: ignore[method-assign]
    return adapter


async def _drain(adapter: EscposPrinterAdapter) -> None:
    if adapter._job_trackers:
        await asyncio.gather(*adapter._job_trackers)


@pytest.fixture
def fast_polling(monkeypatch: Any) -> None:
    monkeypatch.setattr(printer_mod, "JOB_POLL_INTERVAL", 0)


async def test_get_cups_job_state_reads_job_state() -> None:
    sys.modules["pyipp"].IPP.job_state = PROCESSING
    assert await get_cups_job_state("P", 1) == PROCESSING


async def test_completed_job_records_last_print(monkeypatch: Any, fast_polling: None) -> None:
    monkeypatch.setattr(
        printer_mod,
        "get_cups_job_state",
        AsyncMock(side_effect=[PROCESSING, JOB_STATE_COMPLETED]),
    )
    adapter = _adapter(job_id=7)
    seen: list[bool] = []
    adapter.add_print_listener(lambda: seen.append(True))

    await adapter.print_text(HassStub(), text="Hi")
    assert adapter.last_print is None  # submitted, not yet confirmed
    await _drain(adapter)

    assert adapter.last_print is not None
    assert adapter.last_print_job_id == 7
    assert seen == [True]


@pytest.mark.parametrize(
    ("state", "word"), [(JOB_STATE_CANCELED, "canceled"), (JOB_STATE_ABORTED, "aborted")]
)
async def test_failed_job_is_not_counted(
    monkeypatch: Any, caplog: Any, state: int, word: str
) -> None:
    monkeypatch.setattr(printer_mod, "get_cups_job_state", AsyncMock(return_value=state))
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        await adapter.print_qr(HassStub(), data="x")
        await _drain(adapter)
    assert adapter.last_print is None
    assert f"was {word}" in caplog.text


async def test_job_still_pending_at_deadline_is_not_counted(
    monkeypatch: Any, caplog: Any, fast_polling: None
) -> None:
    monkeypatch.setattr(printer_mod, "JOB_TRACK_TIMEOUT", 0)
    monkeypatch.setattr(printer_mod, "get_cups_job_state", AsyncMock(return_value=PROCESSING))
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        await adapter.print_text(HassStub(), text="Hi")
        await _drain(adapter)
    assert adapter.last_print is None
    assert "had not completed" in caplog.text


async def test_state_query_errors_keep_polling(monkeypatch: Any, fast_polling: None) -> None:
    monkeypatch.setattr(
        printer_mod,
        "get_cups_job_state",
        AsyncMock(side_effect=[ConnectionError("blip"), JOB_STATE_COMPLETED]),
    )
    adapter = _adapter()
    await adapter.print_text(HassStub(), text="Hi")
    await _drain(adapter)
    assert adapter.last_print is not None


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("feed", {"lines": 1}), ("cut", {"mode": "full"}), ("beep", {})],
)
async def test_feed_cut_beep_are_not_prints(method: str, kwargs: dict[str, Any]) -> None:
    adapter = _adapter()
    await getattr(adapter, method)(HassStub(), **kwargs)
    assert not adapter._job_trackers
    assert adapter.last_print is None


async def test_missing_job_id_skips_tracking() -> None:
    adapter = _adapter(job_id=0)
    await adapter.print_text(HassStub(), text="Hi")
    assert not adapter._job_trackers


async def test_stop_cancels_trackers(monkeypatch: Any) -> None:
    monkeypatch.setattr(printer_mod, "get_cups_job_state", AsyncMock(return_value=PROCESSING))
    adapter = _adapter()
    await adapter.print_text(HassStub(), text="Hi")
    (task,) = adapter._job_trackers
    await adapter.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not adapter._job_trackers


async def _setup_entry(hass):  # type: ignore[no-untyped-def]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TestPrinter",
        data={CONF_PRINTER_NAME: "TestPrinter"},
        unique_id="cups_TestPrinter",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_sensor_updates_when_print_completes(hass):  # type: ignore[no-untyped-def]
    await _setup_entry(hass)
    state = hass.states.get(SENSOR_ID)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["device_class"] == "timestamp"

    await hass.services.async_call(DOMAIN, "print_text", {"text": "Hi"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(SENSOR_ID)
    assert state.state not in ("unknown", "unavailable")
    assert state.attributes["job_id"] == 1


async def test_sensor_restores_value_after_restart(hass):  # type: ignore[no-untyped-def]
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(SENSOR_ID, "2026-10-01T08:30:00+00:00", {"job_id": 41}),
                {
                    "native_value": {
                        "__type": "<class 'datetime.datetime'>",
                        "isoformat": "2026-10-01T08:30:00+00:00",
                    },
                    "native_unit_of_measurement": None,
                },
            )
        ],
    )
    await _setup_entry(hass)
    state = hass.states.get(SENSOR_ID)
    assert state.state == "2026-10-01T08:30:00+00:00"
    assert state.attributes["job_id"] == 41

"""Direct unit tests for printer-module helpers and adapter internals."""

from __future__ import annotations

import sys
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest

from custom_components.escpos_printer import printer as printer_mod
from custom_components.escpos_printer.printer import (
    CupsError,
    EscposPrinterAdapter,
    PrinterConfig,
    _build_printer_uri,
    _build_root_uri,
    _ipp_timeout,
    _is_connection_error,
    async_check_cups,
    get_cups_printer_status,
    is_cups_available,
)


class HassStub:
    async def async_add_executor_job(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def test_uri_builders() -> None:
    assert _build_printer_uri("Foo") == "ipp://localhost:631/printers/Foo"
    assert _build_printer_uri("Foo", "srv") == "ipp://srv:631/printers/Foo"
    assert _build_printer_uri("Foo", "srv:9100") == "ipp://srv:9100/printers/Foo"
    assert _build_root_uri() == "ipp://localhost:631/"
    assert _build_root_uri("srv:80") == "ipp://srv:80/"


def test_ipp_timeout_rounding() -> None:
    assert _ipp_timeout(4.0) == 4
    assert _ipp_timeout(7.4) == 7
    assert _ipp_timeout(0.2) == 1
    assert _ipp_timeout(0) == 1


def test_is_connection_error_classification() -> None:
    class IPPConnectionError(Exception):
        pass

    assert _is_connection_error(ConnectionError())
    assert _is_connection_error(TimeoutError())
    assert _is_connection_error(OSError())
    assert _is_connection_error(IPPConnectionError())
    assert not _is_connection_error(ValueError("bad request"))


def test_map_helpers() -> None:
    assert EscposPrinterAdapter._map_align(None) == "left"
    assert EscposPrinterAdapter._map_align("CENTER") == "center"
    assert EscposPrinterAdapter._map_align("diagonal") == "left"
    assert EscposPrinterAdapter._map_underline("double") == 2
    assert EscposPrinterAdapter._map_underline(None) == 0
    assert EscposPrinterAdapter._map_underline("wavy") == 0
    assert EscposPrinterAdapter._map_multiplier(None) == 1
    assert EscposPrinterAdapter._map_multiplier("double") == 2
    assert EscposPrinterAdapter._map_multiplier("5") == 5
    assert EscposPrinterAdapter._map_multiplier(99) == 8
    assert EscposPrinterAdapter._map_multiplier("huge") == 1
    assert EscposPrinterAdapter._map_cut("partial") == "PART"
    assert EscposPrinterAdapter._map_cut("full") == "FULL"
    assert EscposPrinterAdapter._map_cut("none") is None
    assert EscposPrinterAdapter._map_cut(None) is None
    assert EscposPrinterAdapter._map_cut("jagged") is None


def test_wrap_text_preserves_newlines() -> None:
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P", line_width=10))
    wrapped = adapter._wrap_text("aaaa bbbb cccc\n\ntail\n")
    assert "aaaa bbbb" in wrapped
    assert wrapped.endswith("\n")
    # Zero width disables wrapping
    adapter_nowrap = EscposPrinterAdapter(PrinterConfig(printer_name="P", line_width=0))
    assert adapter_nowrap._wrap_text("abc def") == "abc def"


class _RaisingIPP:
    """Fake pyipp.IPP whose operations raise a configurable exception."""

    exc: Exception = ConnectionError("refused")

    def __init__(self, uri: str, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def raw(self, *args: Any) -> bytes:
        raise type(self).exc

    async def execute(self, *args: Any) -> Any:
        raise type(self).exc

    async def printer(self) -> Any:
        raise type(self).exc


@pytest.fixture
def raising_ipp(monkeypatch: Any) -> type[_RaisingIPP]:
    monkeypatch.setattr(sys.modules["pyipp"], "IPP", _RaisingIPP)
    return _RaisingIPP


async def test_async_check_cups_connect_reason(raising_ipp: type[_RaisingIPP]) -> None:
    raising_ipp.exc = ConnectionError("refused")
    with pytest.raises(CupsError) as excinfo:
        await async_check_cups("srv")
    assert excinfo.value.reason == "connect"
    assert not await is_cups_available("srv")


async def test_async_check_cups_other_reason(raising_ipp: type[_RaisingIPP]) -> None:
    raising_ipp.exc = ValueError("bad response")
    with pytest.raises(CupsError) as excinfo:
        await async_check_cups("srv")
    assert excinfo.value.reason == "other"


async def test_get_cups_printers_error_returns_empty(raising_ipp: type[_RaisingIPP]) -> None:
    raising_ipp.exc = ConnectionError("refused")
    assert await printer_mod.get_cups_printers("srv") == []
    assert not await printer_mod.is_cups_printer_available("P", "srv")


async def test_get_cups_printer_status_error(raising_ipp: type[_RaisingIPP]) -> None:
    raising_ipp.exc = ConnectionError("refused")
    ok, err = await get_cups_printer_status("P", "srv")
    assert ok is False
    assert "refused" in str(err)


async def test_get_cups_printer_status_stopped(monkeypatch: Any) -> None:
    class _State:
        printer_state = "stopped"
        reasons = "out of paper"

    class _Printer:
        state = _State()

    class _StoppedIPP(_RaisingIPP):
        async def printer(self) -> Any:
            return _Printer()

    monkeypatch.setattr(sys.modules["pyipp"], "IPP", _StoppedIPP)
    ok, err = await get_cups_printer_status("P")
    assert ok is False
    assert "out of paper" in str(err)


async def test_status_check_updates_diagnostics_and_listeners(monkeypatch: Any) -> None:
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P"))
    seen: list[bool] = []
    remove = adapter.add_status_listener(seen.append)

    monkeypatch.setattr(
        printer_mod, "get_cups_printer_status", AsyncMock(return_value=(False, "stopped"))
    )
    await adapter.async_request_status_check(HassStub())
    assert adapter.get_status() is False
    assert seen == [False]
    diag = adapter.get_diagnostics()
    assert diag["last_error_reason"] == "stopped"
    assert diag["last_check"] is not None

    monkeypatch.setattr(
        printer_mod, "get_cups_printer_status", AsyncMock(return_value=(True, None))
    )
    await adapter.async_request_status_check(HassStub())
    assert adapter.get_status() is True
    assert seen == [False, True]

    remove()
    assert not adapter._status_listeners


def test_response_job_id_reads_pyipp_dict() -> None:
    # pyipp's execute() returns a dict; "jobs" is a list of attribute dicts.
    assert printer_mod._response_job_id({"jobs": [{"job-id": 42}]}) == 42
    assert printer_mod._response_job_id({"jobs": []}) == 0
    assert printer_mod._response_job_id({}) == 0


async def test_submit_to_cups_returns_job_id() -> None:
    assert await printer_mod._submit_to_cups("P", b"data") == 1


async def test_get_cups_printers_reads_printer_names() -> None:
    assert await printer_mod.get_cups_printers() == ["TestPrinter"]

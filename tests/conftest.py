from collections.abc import Generator
from enum import IntEnum
import sys
import threading
import types
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    return


@pytest.fixture(autouse=True)
def fake_escpos_module(request: Any) -> Generator[None, None, None]:
    # Do not stub escpos for integration tests; use real CUPS path
    if request.node.get_closest_marker("integration"):
        yield
        return
    escpos = types.ModuleType("escpos")
    printer = types.ModuleType("escpos.printer")

    class _FakeDummyPrinter:
        """Fake Dummy printer that collects ESC/POS commands."""

        def __init__(self, *_, **__):  # type: ignore[no-untyped-def]
            self._buffer = b""

        @property
        def output(self) -> bytes:
            """Return accumulated ESC/POS data."""
            return self._buffer

        def set(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1b@"  # ESC @ (initialize)

        def text(self, text: str = "", *_: Any, **__: Any) -> None:
            self._buffer += text.encode("utf-8", errors="replace")

        def qr(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1dQR"  # Fake QR command

        def image(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1dIMG"  # Fake image command

        def control(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\n"

        def cut(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1dV"  # ESC/POS cut

        def ln(self, lines: int = 1) -> None:
            self._buffer += b"\n" * lines

        def barcode(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1dBC"  # Fake barcode

        def buzzer(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1bBZ"  # Fake buzzer

        def beep(self, *_: Any, **__: Any) -> None:
            self._buffer += b"\x1bBZ"  # Fake buzzer

        def charcode(self, *_: Any, **__: Any) -> None:
            pass

        def close(self) -> None:
            pass

        def _set_codepage(self, *_, **__):  # type: ignore[no-untyped-def]
            pass

        def _raw(self, data: bytes = b"", *_, **__):  # type: ignore[no-untyped-def]
            self._buffer += data

    printer.Dummy = _FakeDummyPrinter  # type: ignore[attr-defined]
    escpos.printer = printer  # type: ignore[attr-defined]

    sys.modules.setdefault("escpos", escpos)
    sys.modules.setdefault("escpos.printer", printer)
    yield


@pytest.fixture(autouse=True)
def fake_pyipp_module(request: Any) -> Generator[None, None, None]:
    """Provide a fake pyipp module for unit tests."""
    if request.node.get_closest_marker("integration"):
        yield
        return

    class _IppPrinterState(IntEnum):
        IDLE = 3
        PROCESSING = 4
        STOPPED = 5

    class _IppJobState(IntEnum):
        PENDING = 0x03
        HELD = 0x04
        PROCESSING = 0x05
        STOPPED = 0x06
        CANCELED = 0x07
        ABORTED = 0x08
        COMPLETED = 0x09

    class _IppOperation(IntEnum):
        PRINT_JOB = 0x0002
        GET_JOB_ATTRIBUTES = 0x0009
        CUPS_GET_DEFAULT = 0x4001
        CUPS_GET_PRINTERS = 0x4002
        GET_PRINTER_ATTRIBUTES = 0x000B

    class _FakeAttribute:
        def __init__(self, value: Any) -> None:
            self.value = value

    class _FakeGroup:
        def __init__(self, attributes: dict[str, Any]) -> None:
            self.attributes = attributes

    class _FakeState:
        def __init__(self) -> None:
            self.printer_state = "idle"
            self.reasons = "none"

    class _FakePrinterInfo:
        def __init__(self) -> None:
            self.name = "TestPrinter"

    class _FakePrinter:
        def __init__(self) -> None:
            self.state = _FakeState()
            self.info = _FakePrinterInfo()

    class _FakeIPP:
        last_request_timeout: int | None = None
        # Reported by Get-Job-Attributes; tests override per case.
        job_state: int = _IppJobState.COMPLETED

        def __init__(self, uri: str, **kwargs: Any) -> None:
            self._uri = uri
            type(self).last_request_timeout = kwargs.get("request_timeout")

        async def __aenter__(self) -> "_FakeIPP":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def printer(self) -> _FakePrinter:
            return _FakePrinter()

        async def execute(self, operation: Any, request_data: Any) -> dict[str, Any]:
            # Same shape as pyipp's parsed response: a dict whose "jobs" and
            # "printers" keys are lists of attribute dicts.
            resp: dict[str, Any] = {"status-code": 0, "jobs": [], "printers": []}
            if operation == _IppOperation.PRINT_JOB:
                resp["jobs"] = [{"job-id": 1, "job-state": _IppJobState.PENDING}]
            elif operation == _IppOperation.GET_JOB_ATTRIBUTES:
                resp["jobs"] = [{"job-id": 1, "job-state": type(self).job_state}]
            elif operation == _IppOperation.CUPS_GET_PRINTERS:
                resp["printers"] = [{"printer-name": "TestPrinter"}]
            return resp

        async def raw(self, operation: Any, request_data: Any) -> bytes:
            return b""

    pyipp_mod = types.ModuleType("pyipp")
    pyipp_enums_mod = types.ModuleType("pyipp.enums")

    pyipp_mod.IPP = _FakeIPP  # type: ignore[attr-defined]
    pyipp_enums_mod.IppPrinterState = _IppPrinterState  # type: ignore[attr-defined]
    pyipp_enums_mod.IppOperation = _IppOperation  # type: ignore[attr-defined]
    pyipp_enums_mod.IppJobState = _IppJobState  # type: ignore[attr-defined]

    orig_pyipp = sys.modules.get("pyipp")
    orig_pyipp_enums = sys.modules.get("pyipp.enums")
    sys.modules["pyipp"] = pyipp_mod
    sys.modules["pyipp.enums"] = pyipp_enums_mod

    yield

    if orig_pyipp is None:
        sys.modules.pop("pyipp", None)
    else:
        sys.modules["pyipp"] = orig_pyipp
    if orig_pyipp_enums is None:
        sys.modules.pop("pyipp.enums", None)
    else:
        sys.modules["pyipp.enums"] = orig_pyipp_enums


@pytest.fixture(autouse=True)
def disable_platform_forwarding_for_unit_tests(monkeypatch: Any, request: Any) -> None:
    """Avoid starting HA http/notify stack in unit tests.

    For tests not marked as 'integration', prevent platform forwarding by
    setting PLATFORMS to an empty list. Service registration remains intact.
    """
    if request.node.get_closest_marker("integration"):
        return
    try:
        import custom_components.escpos_printer.__init__ as cc_init

        # Allow notify platform during unit tests; disable others via env flag
        monkeypatch.setattr(cc_init, "PLATFORMS", ["notify"], raising=False)
        monkeypatch.setenv("ESC_POS_DISABLE_PLATFORMS", "0")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def stub_http_component_for_unit_tests(monkeypatch: Any, request: Any) -> None:
    """Provide a minimal stub for the Home Assistant http component in unit tests.

    The real http component can spawn background threads; stubbing avoids lingering
    thread assertions in unit tests. Integration tests use the real component.
    """
    if request.node.get_closest_marker("integration"):
        return
    mod = types.ModuleType("homeassistant.components.http")
    async def _ok(*args: Any, **kwargs: Any) -> bool:
        return True
    # Provide the setup entrypoints expected by HA
    mod.async_setup = _ok  # type: ignore[attr-defined]
    mod.async_setup_entry = _ok  # type: ignore[attr-defined]
    mod.async_unload_entry = _ok  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.http", mod)


@pytest.fixture(autouse=True)
def avoid_safe_shutdown_thread(monkeypatch: Any, request: Any) -> None:
    """Prevent Home Assistant's safe-shutdown background thread in unit tests.

    Intercepts thread starts whose target function is named '_run_safe_shutdown_loop'
    and short-circuits them. This avoids lingering thread assertions from the
    test harness. Integration tests keep the real behavior.
    """
    if request.node.get_closest_marker("integration"):
        return

    _orig_start = threading.Thread.start

    def _patched_start(self: Any, *args: Any, **kwargs: Any) -> Any:
        target_name = getattr(getattr(self, "_target", None), "__name__", None)
        if target_name == "_run_safe_shutdown_loop":
            # Do not start this thread in unit tests
            return None
        return _orig_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", _patched_start, raising=True)

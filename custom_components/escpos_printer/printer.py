from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import io
import logging
import textwrap
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from PIL import Image

from .const import DEFAULT_ALIGN, DEFAULT_CUT, DEFAULT_TIMEOUT, DENSITY_LEVELS, TEXT_FONTS
from .security import (
    MAX_BEEP_TIMES,
    MAX_FEED_LINES,
    sanitize_log_message,
    validate_barcode_data,
    validate_image_url,
    validate_local_image_path,
    validate_numeric_input,
    validate_qr_data,
    validate_text_input,
    validate_timeout,
)

_LOGGER = logging.getLogger(__name__)


class CupsError(Exception):
    """A CUPS server check failed.

    ``reason`` is a coarse machine-readable category:
    ``pyipp_missing`` (IPP client library not installed),
    ``connect`` (server unreachable or timed out), or ``other``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _is_connection_error(err: Exception) -> bool:
    """Classify *err* as a connectivity failure without importing pyipp exceptions.

    Matched by type name so the check also works when pyipp is stubbed in tests.
    """
    if isinstance(err, TimeoutError | ConnectionError | OSError):
        return True
    name = type(err).__name__
    return "Connection" in name or "Timeout" in name or "ClientError" in name


def parse_density(value: Any) -> int | None:
    """Map a density percentage ("+25", "-12.5%", 25, ...) to python-escpos's index.

    None/"" means "leave the printer's darkness unchanged".

    Raises:
        ValueError: if *value* is not one of the DENSITY_LEVELS percentages.
    """
    if value is None or value == "":
        return None
    try:
        pct: float | None = float(str(value).strip().removesuffix("%"))
    except ValueError:
        pct = None
    for label, index in DENSITY_LEVELS.items():
        if pct is not None and float(label) == pct:
            return index
    raise ValueError(f"density must be one of {', '.join(DENSITY_LEVELS)} (percent)")


def density_value(value: Any) -> Any:
    """Voluptuous validator: accept only values parse_density understands."""
    parse_density(value)
    return value


def _map_font(font: Any) -> str:
    """Normalize a font choice to python-escpos's "a"/"b"; anything else is A."""
    font_s = str(font).strip().lower() if font is not None else "a"
    font_s = {"0": "a", "1": "b"}.get(font_s, font_s)
    return font_s if font_s in TEXT_FONTS else "a"


def _apply_font(printer: Any, font: str) -> str:
    """Select *font* on *printer*; return the font actually in effect.

    python-escpos raises NotSupported when the printer profile has no such
    font, in which case font A is used instead.
    """
    try:
        printer.set(font=font)
    except Exception as e:
        if font == "a":
            _LOGGER.debug("Font A selection failed: %s", sanitize_log_message(str(e)))
            return "a"
        _LOGGER.warning(
            "Font %s is not supported by this printer profile; using font A", font.upper()
        )
        with contextlib.suppress(Exception):
            printer.set(font="a")
        return "a"
    return font


# Late import of python-escpos to avoid import errors at HA startup if deps pending
def _get_dummy_printer() -> type[Any]:
    """Get the Dummy printer class for building ESC/POS commands."""
    from escpos.printer import Dummy  # noqa: PLC0415

    return Dummy  # type: ignore[no-any-return]


# Fallback resize cap when neither a per-entry width override nor a
# profile-declared width is available (pre-existing behavior).
_MAX_IMAGE_WIDTH = 512

# Pillow moved resampling filters into Image.Resampling in 9.1
_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


def _resize_if_wide(img: Image.Image, max_width: int = _MAX_IMAGE_WIDTH) -> Image.Image:
    """Scale *img* down to the printable width, keeping aspect ratio.

    Blocking (PIL); run in an executor.
    """
    try:
        orig_w, orig_h = img.width, img.height
        if orig_w > max_width:
            ratio = max_width / float(orig_w)
            new_size = (max_width, int(orig_h * ratio))
            img = img.resize(new_size, _LANCZOS)
            _LOGGER.debug("Resized image from %sx%s to %sx%s", orig_w, orig_h, new_size[0], new_size[1])
    except Exception as e:
        _LOGGER.debug("Image resize failed, printing original size: %s", sanitize_log_message(str(e)))
    return img


def _decode_image_bytes(content: bytes, max_width: int = _MAX_IMAGE_WIDTH) -> Image.Image:
    """Decode downloaded image bytes and resize if needed.

    Blocking (PIL); run in an executor.
    """
    img = Image.open(io.BytesIO(content))
    img.load()
    return _resize_if_wide(img, max_width)


def _load_image_file(path: str, max_width: int = _MAX_IMAGE_WIDTH) -> Image.Image:
    """Open a local image file and resize if needed.

    Blocking (PIL); run in an executor.
    """
    img = Image.open(path)
    img.load()
    return _resize_if_wide(img, max_width)


# Cache of the kwarg names each printer class's image() accepts, probed via
# inspect.signature. None means unintrospectable or **kwargs — pass everything.
_IMAGE_KWARGS_CACHE: dict[type, frozenset[str] | None] = {}


def _supported_image_kwargs(printer: Any) -> frozenset[str] | None:
    """Return the kwarg names *printer*.image() accepts, or None to pass all."""
    cls = type(printer)
    if cls in _IMAGE_KWARGS_CACHE:
        return _IMAGE_KWARGS_CACHE[cls]
    supported: frozenset[str] | None
    try:
        import inspect  # noqa: PLC0415

        params = inspect.signature(printer.image).parameters.values()
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            supported = None
        else:
            supported = frozenset(p.name for p in params)
    except (TypeError, ValueError):
        supported = None
    _IMAGE_KWARGS_CACHE[cls] = supported
    return supported


def _ipp_timeout(timeout: float) -> int:
    """Convert the configured timeout (float seconds) to pyipp's int request_timeout.

    Sub-second values round up to 1 second, pyipp's minimum granularity.
    """
    return max(1, round(timeout))


def _build_printer_uri(printer_name: str, server: str | None = None) -> str:
    """Build an IPP URI for a CUPS printer queue."""
    host = server or "localhost"
    if ":" not in host:
        host = f"{host}:631"
    return f"ipp://{host}/printers/{printer_name}"


def _build_root_uri(server: str | None = None) -> str:
    """Build an IPP URI for the CUPS server root."""
    host = server or "localhost"
    if ":" not in host:
        host = f"{host}:631"
    return f"ipp://{host}/"


async def _submit_to_cups(
    printer_name: str,
    data: bytes,
    server: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Submit raw ESC/POS bytes to a CUPS printer via IPP.

    Args:
        printer_name: CUPS printer queue name.
        data: Raw ESC/POS bytes to send.
        server: CUPS server address ('host' or 'host:port'). None means localhost.
        timeout: IPP request timeout in seconds.

    Returns:
        IPP job ID.
    """
    from pyipp import IPP  # noqa: PLC0415
    from pyipp.enums import IppOperation  # noqa: PLC0415

    uri = _build_printer_uri(printer_name, server)
    async with IPP(uri, request_timeout=_ipp_timeout(timeout)) as ipp:
        response = await ipp.execute(
            IppOperation.PRINT_JOB,
            {
                "operation-attributes-tag": {
                    "requesting-user-name": "homeassistant",
                    "job-name": "ESC/POS Print Job",
                    "document-format": "application/vnd.cups-raw",
                },
                "data": data,
            },
        )
    job_id = _response_job_id(response)
    _LOGGER.debug("Submitted IPP job %s to printer '%s'", job_id, printer_name)
    return job_id


def _response_job_id(response: dict[str, Any]) -> int:
    """Return the job-id from a parsed pyipp response, or 0 if absent.

    pyipp's ``execute`` returns a plain dict whose "jobs" key is a list of
    per-job attribute dicts.
    """
    for job in response.get("jobs") or []:
        job_id = job.get("job-id")
        if job_id:
            return int(job_id)
    return 0


# Encoded in the sample receipt's QR code
SAMPLE_QR_URL = "https://github.com/mglatt/ha-escpos-thermal-printer-cups"

# IPP job-state values (RFC 8011 5.3.7) used to follow a submitted job.
JOB_STATE_CANCELED = 7
JOB_STATE_ABORTED = 8
JOB_STATE_COMPLETED = 9
_JOB_STATE_NAMES = {JOB_STATE_CANCELED: "canceled", JOB_STATE_ABORTED: "aborted"}

# How long to follow a submitted job before giving up on confirming it. A
# raw receipt completes within seconds; a job still pending after this is
# most likely waiting on an offline or stopped printer.
JOB_TRACK_TIMEOUT = 120.0
JOB_POLL_INTERVAL = 2.0


async def get_cups_job_state(
    printer_name: str,
    job_id: int,
    server: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> int | None:
    """Return the IPP job-state of *job_id* on *printer_name*, or None if not reported."""
    from pyipp import IPP  # noqa: PLC0415
    from pyipp.enums import IppOperation  # noqa: PLC0415

    uri = _build_printer_uri(printer_name, server)
    async with IPP(uri, request_timeout=_ipp_timeout(timeout)) as ipp:
        response = await ipp.execute(
            IppOperation.GET_JOB_ATTRIBUTES,
            {
                "operation-attributes-tag": {
                    "requesting-user-name": "homeassistant",
                    "job-id": job_id,
                    "requested-attributes": ["job-state"],
                },
            },
        )
    for job in response.get("jobs") or []:
        state = job.get("job-state")
        if state is not None:
            return int(state)
    return None


async def async_check_cups(server: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Probe the CUPS server at *server*; raise CupsError with a reason on failure.

    Args:
        server: CUPS server address. None means localhost.
        timeout: IPP request timeout in seconds.

    Raises:
        CupsError: with reason "pyipp_missing", "connect", or "other".
    """
    try:
        from pyipp import IPP  # noqa: PLC0415
        from pyipp.enums import IppOperation  # noqa: PLC0415
    except ImportError as e:
        _LOGGER.warning("pyipp library not available — CUPS printing disabled")
        raise CupsError("pyipp_missing", str(e)) from e

    uri = _build_root_uri(server)
    try:
        async with IPP(uri, request_timeout=_ipp_timeout(timeout)) as ipp:
            await ipp.raw(
                IppOperation.CUPS_GET_PRINTERS,
                {"operation-attributes-tag": {}},
            )
    except Exception as e:
        reason = "connect" if _is_connection_error(e) else "other"
        _LOGGER.warning(
            "CUPS server check failed (%s): %s",
            reason,
            sanitize_log_message(str(e)),
            exc_info=True,
        )
        raise CupsError(reason, str(e)) from e


async def is_cups_available(server: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Return True if the CUPS server at *server* is reachable via IPP.

    Args:
        server: CUPS server address. None means localhost.
        timeout: IPP request timeout in seconds.

    Returns:
        True if the server responds to IPP requests.
    """
    try:
        await async_check_cups(server, timeout)
    except CupsError:
        return False
    return True


async def get_cups_printers(server: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Return printer names registered on the CUPS server.

    Args:
        server: CUPS server address. None means localhost.
        timeout: IPP request timeout in seconds.

    Returns:
        List of CUPS printer queue names.
    """
    try:
        from pyipp import IPP  # noqa: PLC0415
        from pyipp.enums import IppOperation  # noqa: PLC0415

        uri = _build_root_uri(server)
        async with IPP(uri, request_timeout=_ipp_timeout(timeout)) as ipp:
            response = await ipp.execute(
                IppOperation.CUPS_GET_PRINTERS,
                {"operation-attributes-tag": {}},
            )
        # pyipp returns a dict; "printers" is a list of per-printer
        # attribute dicts keyed by IPP attribute name.
        return [
            str(p["printer-name"])
            for p in response.get("printers") or []
            if p.get("printer-name")
        ]
    except ImportError:
        _LOGGER.warning("pyipp library not available")
        return []
    except Exception as e:
        _LOGGER.warning(
            "Failed to get CUPS printers: %s", sanitize_log_message(str(e)), exc_info=True
        )
        return []


async def is_cups_printer_available(
    printer_name: str,
    server: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Return True if *printer_name* exists on the CUPS server.

    Args:
        printer_name: CUPS queue name.
        server: CUPS server address. None means localhost.
        timeout: IPP request timeout in seconds.

    Returns:
        True if the printer responds to Get-Printer-Attributes.
    """
    try:
        from pyipp import IPP  # noqa: PLC0415

        uri = _build_printer_uri(printer_name, server)
        async with IPP(uri, request_timeout=_ipp_timeout(timeout)) as ipp:
            await ipp.printer()
        return True
    except ImportError:
        _LOGGER.warning("pyipp library not available")
        return False
    except Exception as e:
        _LOGGER.warning("Failed to check CUPS printer: %s", sanitize_log_message(str(e)))
        return False


async def get_cups_printer_status(
    printer_name: str,
    server: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str | None]:
    """Return (is_available, error_message) for *printer_name*.

    Args:
        printer_name: CUPS queue name.
        server: CUPS server address. None means localhost.
        timeout: IPP request timeout in seconds.

    Returns:
        Tuple of (printer_ok, error_message). error_message is None when ok.
    """
    try:
        from pyipp import IPP  # noqa: PLC0415

        uri = _build_printer_uri(printer_name, server)
        async with IPP(uri, request_timeout=_ipp_timeout(timeout)) as ipp:
            printer = await ipp.printer()

        state = getattr(printer, "state", None)
        if state:
            printer_state = getattr(state, "printer_state", None)
            reasons = getattr(state, "reasons", None)
            if printer_state and "stopped" in str(printer_state).lower():
                reason = reasons if reasons else "Printer stopped"
                return False, str(reason)

        return True, None
    except ImportError:
        return False, "pyipp library not available"
    except Exception as e:
        return False, str(e)



@dataclass
class PrinterConfig:
    printer_name: str
    cups_server: str | None = None
    timeout: float = 4.0
    codepage: str | None = None
    profile: str | None = None
    line_width: int = 48
    # Per-entry image width override in dots; None = profile width or fallback
    width_pixels: int | None = None
    # Resolved default image implementation (a python-escpos impl name);
    # None = let python-escpos use its own default
    impl: str | None = None


class EscposPrinterAdapter:
    def __init__(self, config: PrinterConfig) -> None:
        self._config = config
        # Validate timeout eagerly
        self._config.timeout = validate_timeout(self._config.timeout)
        self._status_interval: int = 0
        self._lock = asyncio.Lock()
        self._cancel_status: Callable[[], None] | None = None
        self._status: bool | None = None
        self._status_listeners: list[Callable[[bool], None]] = []
        self._last_check: Any = None
        self._last_ok: Any = None
        self._last_error: Any = None
        self._last_latency_ms: int | None = None
        self._last_error_reason: str | None = None
        self._no_image_warned = False
        # Last print CUPS confirmed as completed (Last print sensor)
        self._last_print: Any = None
        self._last_print_job_id: int | None = None
        self._print_listeners: list[Callable[[], None]] = []
        self._job_trackers: set[asyncio.Task[None]] = set()

    @property
    def config(self) -> PrinterConfig:
        """Return the printer configuration."""
        return self._config

    # Utilities
    def _connect(self) -> Any:
        """Create a Dummy printer to collect ESC/POS commands.

        The Dummy printer buffers all ESC/POS commands. When operations are complete,
        the buffered data is submitted to CUPS via _submit_job().
        """
        _LOGGER.debug("Creating Dummy printer for CUPS submission to: %s", self._config.printer_name)
        dummy_class = _get_dummy_printer()
        profile_name: str | None = None
        if self._config.profile:
            try:
                from .capabilities import resolve_profile_name  # noqa: PLC0415
                from .custom_profiles import register_custom_profiles  # noqa: PLC0415

                # Custom profiles (RP820, TM-m30III, ...) must be in the
                # registry before Escpos resolves the name, and stored
                # clone aliases resolve to their real profile key here so
                # python-escpos only ever sees names it knows.
                register_custom_profiles()
                profile_name = resolve_profile_name(self._config.profile) or self._config.profile
            except Exception as e:
                _LOGGER.debug(
                    "Could not resolve printer profile '%s': %s",
                    self._config.profile,
                    sanitize_log_message(str(e)),
                )
                profile_name = self._config.profile

        # Escpos.__init__ takes the profile *name* and resolves it itself.
        try:
            printer = dummy_class(profile=profile_name)
        except Exception as e:
            _LOGGER.warning(
                "Unknown printer profile '%s', printing without a profile: %s",
                profile_name,
                sanitize_log_message(str(e)),
            )
            printer = dummy_class()
        _LOGGER.debug("Dummy printer created: %s", printer)
        return printer

    async def _submit_job(self, printer: Any) -> int | None:
        """Submit the Dummy printer's output to CUPS via IPP.

        Args:
            printer: The Dummy printer instance with buffered ESC/POS data.

        Returns:
            IPP job ID, or None if no data to print.
        """
        data = printer.output
        if not data:
            _LOGGER.debug("No data to submit to CUPS")
            return None

        _LOGGER.debug("Submitting %d bytes to CUPS printer '%s'", len(data), self._config.printer_name)
        job_id = await _submit_to_cups(
            self._config.printer_name,
            data,
            self._config.cups_server,
            timeout=self._config.timeout,
        )
        return job_id

    async def start(self, hass: HomeAssistant, *, status_interval: int) -> None:
        self._status_interval = max(0, int(status_interval))

        # Schedule status checks
        if self._status_interval > 0:
            from datetime import timedelta  # noqa: PLC0415

            from homeassistant.helpers.event import async_track_time_interval  # noqa: PLC0415

            async def _tick(now: Any) -> None:
                await self._status_check(hass)

            self._cancel_status = async_track_time_interval(hass, _tick, timedelta(seconds=self._status_interval))
        # Perform an initial status probe only when status checks are enabled
        if self._status_interval > 0:
            await self._status_check(hass)

    async def stop(self) -> None:
        if self._cancel_status:
            self._cancel_status()
        self._cancel_status = None
        for task in list(self._job_trackers):
            task.cancel()
        self._job_trackers.clear()

    @property
    def last_print(self) -> Any:
        """When CUPS last reported one of our print jobs completed (UTC), or None."""
        return self._last_print

    @property
    def last_print_job_id(self) -> int | None:
        """CUPS job ID of the print behind ``last_print``."""
        return self._last_print_job_id

    def add_print_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call *callback* whenever ``last_print`` changes; returns an unsubscribe."""
        self._print_listeners.append(callback)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._print_listeners.remove(callback)

        return _remove

    def _start_job_tracking(self, hass: HomeAssistant, job_id: int) -> None:
        """Follow *job_id* in the background until CUPS reports a final state."""
        coro = self._track_job(job_id)
        create = getattr(hass, "async_create_background_task", None)
        if create is not None:
            task = create(coro, f"escpos_printer track CUPS job {job_id}")
        else:  # plain-asyncio callers (unit tests)
            task = asyncio.get_running_loop().create_task(coro)
        self._job_trackers.add(task)
        task.add_done_callback(self._job_trackers.discard)

    async def _track_job(self, job_id: int) -> None:
        """Poll CUPS until *job_id* completes, fails, or JOB_TRACK_TIMEOUT passes.

        Only "completed" counts as printed: for a raw queue that means the
        CUPS backend delivered every byte to the printer. CUPS keeps job
        history by default, so a job that finished between polls is still
        reported.
        """
        name = self._config.printer_name
        deadline = time.monotonic() + JOB_TRACK_TIMEOUT
        while True:
            try:
                state = await get_cups_job_state(
                    name, job_id, self._config.cups_server, timeout=self._config.timeout
                )
            except Exception as e:
                _LOGGER.debug("Job %s state query failed: %s", job_id, sanitize_log_message(str(e)))
                state = None
            if state == JOB_STATE_COMPLETED:
                self._record_print(job_id)
                return
            if state in _JOB_STATE_NAMES:
                _LOGGER.warning(
                    "CUPS job %s on '%s' was %s; not counted as printed",
                    job_id,
                    name,
                    _JOB_STATE_NAMES[state],
                )
                return
            if time.monotonic() >= deadline:
                _LOGGER.warning(
                    "CUPS job %s on '%s' had not completed after %ds (printer offline?); "
                    "not counted as printed",
                    job_id,
                    name,
                    int(JOB_TRACK_TIMEOUT),
                )
                return
            await asyncio.sleep(JOB_POLL_INTERVAL)

    def _record_print(self, job_id: int) -> None:
        self._last_print = dt_util.utcnow()
        self._last_print_job_id = job_id
        for cb in list(self._print_listeners):
            with contextlib.suppress(Exception):
                cb()

    async def _status_check(self, hass: HomeAssistant) -> None:
        # CUPS printer status check via IPP (native async, no executor needed)
        start = time.perf_counter()
        ok, err = await get_cups_printer_status(
            self._config.printer_name,
            self._config.cups_server,
            timeout=self._config.timeout,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        now = dt_util.utcnow()
        self._last_check = now
        self._last_latency_ms = latency_ms
        if ok:
            self._last_ok = now
            self._last_error_reason = None
        else:
            self._last_error = now
            self._last_error_reason = sanitize_log_message(err or "unavailable")
        if self._status != ok:
            self._status = ok
            if not ok:
                _LOGGER.warning("CUPS printer '%s' not available: %s", self._config.printer_name, err)
            # Notify listeners
            for cb in list(self._status_listeners):
                with contextlib.suppress(Exception):
                    cb(ok)

    def get_status(self) -> bool | None:
        return self._status

    async def async_request_status_check(self, hass: HomeAssistant) -> None:
        await self._status_check(hass)

    def add_status_listener(self, callback: Callable[[bool], None]) -> Callable[[], None]:
        self._status_listeners.append(callback)
        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._status_listeners.remove(callback)
        return _remove

    def get_diagnostics(self) -> dict[str, Any]:
        def _iso(dt_obj: Any) -> str | None:
            return dt_obj.isoformat() if dt_obj is not None else None
        return {
            "last_check": _iso(self._last_check),
            "last_ok": _iso(self._last_ok),
            "last_error": _iso(self._last_error),
            "last_latency_ms": self._last_latency_ms,
            "last_error_reason": self._last_error_reason,
        }

    def _columns_for_font(self, printer: Any, font: str) -> int:
        """Wrap width for *font*: the configured width, scaled up for font B.

        Font B packs more columns onto the same paper. The ratio comes from
        the profile python-escpos is actually printing with, so a custom
        line width keeps its proportion (e.g. 48 -> 64 on a 48/64 printer).
        """
        base = int(self._config.line_width or 0)
        if font != "b" or base <= 0:
            return base
        try:
            cols_a = printer.profile.get_columns("a")
            cols_b = printer.profile.get_columns("b")
        except Exception:
            return base
        if isinstance(cols_a, int) and isinstance(cols_b, int) and cols_a > 0 and cols_b > 0:
            return max(1, base * cols_b // cols_a)
        return base

    def _wrap_text(self, text: str, cols: int | None = None) -> str:
        """Wrap text to fit within *cols* (default: the configured line width).

        Preserves all newlines including trailing ones. Python's
        str.splitlines() strips trailing newlines, so we detect and
        restore them after wrapping.

        Args:
            text: Text to wrap.
            cols: Column count; None uses the configured line width.

        Returns:
            Wrapped text with original newline structure preserved.
        """
        if cols is None:
            cols = int(self._config.line_width or 0)
        cols = max(0, cols)
        if cols <= 0:
            return text

        # splitlines() strips trailing newlines — count them so we can restore
        trailing_newlines = len(text) - len(text.rstrip("\n"))

        wrapped_lines: list[str] = []
        for line in text.splitlines():
            # Preserve empty lines
            if not line:
                wrapped_lines.append("")
                continue
            wrapped_lines.extend(textwrap.wrap(line, width=cols, replace_whitespace=False, drop_whitespace=False))

        result = "\n".join(wrapped_lines)

        # Restore trailing newlines that splitlines() consumed
        current_trailing = len(result) - len(result.rstrip("\n"))
        needed = trailing_newlines - current_trailing
        if needed > 0:
            result += "\n" * needed

        return result

    @staticmethod
    def _map_align(align: str | None) -> str:
        if not align:
            return DEFAULT_ALIGN
        align = align.lower()
        return align if align in ("left", "center", "right") else DEFAULT_ALIGN

    @staticmethod
    def _map_underline(underline: str | None) -> int:
        mapping = {"none": 0, "single": 1, "double": 2}
        if not underline:
            return 0
        return mapping.get(underline.lower(), 0)

    @staticmethod
    def _map_multiplier(val: str | int | None) -> int:
        mapping = {"normal": 1, "double": 2, "triple": 3}
        if not val:
            return 1
        if isinstance(val, int):
            return max(1, min(8, val))
        if isinstance(val, str):
            if val.isdigit():
                return max(1, min(8, int(val)))
            return mapping.get(val.lower(), 1)
        return 1

    @staticmethod
    def _map_cut(mode: str | None) -> str | None:
        if not mode:
            return None
        mode_l = mode.lower()
        if mode_l == "partial":
            return "PART"
        if mode_l == "full":
            return "FULL"
        if mode_l == "none":
            return None
        return None

    async def _apply_cut_and_feed(self, hass: HomeAssistant, printer: Any, cut: str | None, feed: int | None) -> None:
        # feed first, then cut
        if feed is not None:
            lines = validate_numeric_input(feed, 0, MAX_FEED_LINES, "feed")
            if lines > 0:
                def _feed() -> None:
                    # Some versions have ln(); otherwise send newlines
                    if hasattr(printer, "ln"):
                        printer.ln(lines)
                    else:
                        try:
                            printer._raw(b"\n" * lines)
                        except Exception:
                            for _ in range(lines):
                                printer.text("\n")

                await hass.async_add_executor_job(_feed)

        cut_mode = self._map_cut(cut)
        if cut_mode:
            def _cut() -> None:
                try:
                    printer.cut(mode=cut_mode)
                except Exception as e:
                    _LOGGER.debug("Cut not supported: %s", e)

            await hass.async_add_executor_job(_cut)

    def _mark_success(self) -> None:
        """Record a successful job: the printer is reachable."""
        now = dt_util.utcnow()
        self._status = True
        self._last_ok = now
        self._last_check = now
        for cb in list(self._status_listeners):
            with contextlib.suppress(Exception):
                cb(True)

    async def _run_job(
        self,
        hass: HomeAssistant,
        op_name: str,
        build: Callable[[Any], None],
        *,
        cut: str | None = None,
        feed: int | None = None,
        apply_cut_feed: bool = True,
        track: bool = False,
    ) -> None:
        """Build ESC/POS bytes with *build* and submit them to CUPS as one job.

        Creates a fresh Dummy buffer, runs the (blocking) build function in the
        executor, optionally applies trailing feed/cut, and submits the buffered
        bytes over IPP. Marks the printer reachable on success. With *track*,
        the job is followed until CUPS reports it completed, which updates
        ``last_print``; feed/cut/beep don't count as prints.
        """
        async with self._lock:
            printer = await hass.async_add_executor_job(self._connect)
            try:
                await hass.async_add_executor_job(build, printer)
                if apply_cut_feed:
                    await self._apply_cut_and_feed(hass, printer, cut, feed)
                job_id = await self._submit_job(printer)
                _LOGGER.debug("CUPS job submitted for %s: %s", op_name, job_id)
            except Exception as e:
                _LOGGER.error("%s failed: %s", op_name, sanitize_log_message(str(e)))
                raise
        self._mark_success()
        if track:
            if job_id:
                self._start_job_tracking(hass, job_id)
            else:
                _LOGGER.debug("CUPS returned no job ID for %s; cannot confirm completion", op_name)

    # Operations
    async def print_text(
        self,
        hass: HomeAssistant,
        *,
        text: str,
        align: str | None = None,
        bold: bool | None = None,
        underline: str | None = None,
        width: str | None = None,
        height: str | None = None,
        invert: bool | None = None,
        density: Any = None,
        font: str | None = None,
        encoding: str | None = None,
        cut: str | None = DEFAULT_CUT,
        feed: int | None = 0,
    ) -> None:
        text = validate_text_input(text)
        align_m = self._map_align(align)
        ul = self._map_underline(underline)
        wmult = self._map_multiplier(width)
        hmult = self._map_multiplier(height)
        density_idx = parse_density(density)
        font_v = _map_font(font)

        def _do_full_print(printer: Any) -> None:  # noqa: PLR0912
            """Print text using the provided printer instance."""
            _LOGGER.debug("print_text begin: text=%r, align=%s", text[:50], align_m)
            # Optional codepage
            if self._config.codepage:
                try:
                    if hasattr(printer, "charcode"):
                        printer.charcode(self._config.codepage)
                except Exception as e:
                    _LOGGER.debug("Codepage set failed: %s", sanitize_log_message(str(e)))

            # Set style
            if hasattr(printer, "set"):
                _LOGGER.debug("Setting printer style: align=%s, bold=%s, width=%s, height=%s", align_m, bold, wmult, hmult)
                # invert is always sent: escpos set() emits nothing for None,
                # and jobs carry no ESC @ reset, so printer state persists
                # across CUPS jobs -- one inverted job would otherwise leave
                # every later print white-on-black. density=None stays
                # unsent on purpose: darkness is a hardware knob that should
                # stick until changed.
                if wmult > 1 or hmult > 1:
                    printer.set(
                        align=align_m, bold=bool(bold), underline=ul,
                        width=wmult, height=hmult,
                        custom_size=True, normal_textsize=False,
                        invert=bool(invert), density=density_idx,
                    )
                else:
                    printer.set(align=align_m, bold=bool(bold), underline=ul, width=wmult, height=hmult,
                                custom_size=False, normal_textsize=True,
                                invert=bool(invert), density=density_idx)
                # Font separately: escpos raises NotSupported mid-set() for a
                # font the profile lacks, which would skip the settings above.
                font_used = _apply_font(printer, font_v)
            else:
                font_used = "a"
            text_to_print = self._wrap_text(text, self._columns_for_font(printer, font_used))

            # Encoding is best-effort; python-escpos handles str internally.
            if encoding:
                try:
                    # Try to set codepage if printer exposes helper
                    if hasattr(printer, "_set_codepage"):
                        try:
                            printer._set_codepage(encoding)
                        except Exception:
                            _LOGGER.warning("Unsupported encoding/codepage: %s", encoding)
                    text_bytes = text_to_print.encode(encoding, errors="replace")
                    if hasattr(printer, "_raw"):
                        printer._raw(text_bytes)
                    else:
                        printer.text(text_to_print)
                except Exception as e:
                    _LOGGER.debug("Encoding error, falling back: %s", e)
                    printer.text(text_to_print)
            else:
                _LOGGER.debug("Sending text to printer...")
                printer.text(text_to_print)
                _LOGGER.debug("Text sent to buffer")

        await self._run_job(hass, "print_text", _do_full_print, cut=cut, feed=feed, track=True)

    async def print_qr(
        self,
        hass: HomeAssistant,
        *,
        data: str,
        size: int | None = None,
        ec: str | None = None,
        align: str | None = None,
        cut: str | None = DEFAULT_CUT,
        feed: int | None = 0,
    ) -> None:
        data = validate_qr_data(data)
        align_m = self._map_align(align)
        qsize = int(size) if size is not None else 3
        qsize = max(1, min(16, qsize))
        qec = (ec or "M").upper()
        if qec not in ("L", "M", "Q", "H"):
            qec = "M"
        def _map_qr_ec(level: str) -> Any:
            try:
                from escpos import escpos as _esc  # noqa: PLC0415
                return {
                    "L": getattr(_esc, "QR_ECLEVEL_L", "L"),
                    "M": getattr(_esc, "QR_ECLEVEL_M", "M"),
                    "Q": getattr(_esc, "QR_ECLEVEL_Q", "Q"),
                    "H": getattr(_esc, "QR_ECLEVEL_H", "H"),
                }[level]
            except Exception:
                return level

        def _do_print(printer: Any) -> None:
            if hasattr(printer, "set"):
                # invert=False: undo a white-on-black left by an earlier job
                printer.set(align=align_m, invert=False)
            printer.qr(data, size=qsize, ec=_map_qr_ec(qec))

        await self._run_job(hass, "print_qr", _do_print, cut=cut, feed=feed, track=True)

    def image_target_width(self) -> int:
        """Effective resize target: entry override → profile width → fallback."""
        if self._config.width_pixels:
            return int(self._config.width_pixels)
        try:
            from .capabilities import get_profile_pixel_width  # noqa: PLC0415

            profile_width = get_profile_pixel_width(self._config.profile)
        except Exception:
            profile_width = None
        return profile_width or _MAX_IMAGE_WIDTH

    async def print_image(
        self,
        hass: HomeAssistant,
        *,
        image: str,
        high_density: bool = True,
        align: str | None = None,
        cut: str | None = DEFAULT_CUT,
        feed: int | None = 0,
        impl: str | None = None,
    ) -> None:
        # Resolve image source
        img_obj: Image.Image
        max_width = self.image_target_width()

        if image.lower().startswith(("http://", "https://")):
            _LOGGER.debug("Downloading image from URL: %s", sanitize_log_message(image, ["text", "data"]))
            url = validate_image_url(image)
            session = async_get_clientsession(hass)
            resp = await session.get(url)
            try:
                resp.raise_for_status()
                content = await resp.read()
            finally:
                with contextlib.suppress(Exception):
                    resp.release()
            img_obj = await hass.async_add_executor_job(_decode_image_bytes, content, max_width)
        else:
            _LOGGER.debug("Opening local image: %s", image)
            path = validate_local_image_path(image)
            img_obj = await hass.async_add_executor_job(_load_image_file, path, max_width)

        align_m = self._map_align(align)
        # Service-call impl wins over the per-entry default; None leaves the
        # choice to python-escpos.
        effective_impl = impl or self._config.impl

        # Profile flags are hints, not gates: warn once, print anyway.
        if not self._no_image_warned:
            try:
                from .capabilities import profile_declares_no_images  # noqa: PLC0415

                if profile_declares_no_images(self._config.profile):
                    self._no_image_warned = True
                    _LOGGER.warning(
                        "Profile '%s' declares no image support; printing anyway",
                        self._config.profile,
                    )
            except Exception:  # capability lookup must never block printing
                pass

        def _do_print(printer: Any) -> None:
            if hasattr(printer, "set"):
                # invert=False: undo a white-on-black left by an earlier job
                printer.set(align=align_m, invert=False)
            # Some printers need conversion; python-escpos handles PIL.Image
            if hasattr(printer, "image"):
                kwargs: dict[str, Any] = {
                    "high_density_vertical": high_density,
                    "high_density_horizontal": high_density,
                }
                if effective_impl:
                    kwargs["impl"] = effective_impl
                supported = _supported_image_kwargs(printer)
                if supported is not None:
                    kwargs = {k: v for k, v in kwargs.items() if k in supported}
                printer.image(img_obj, **kwargs)
            else:
                # Fallback: convert to bytes via ESC/POS raster if possible
                printer.text("[image printing not supported by this printer]\n")

        await self._run_job(hass, "print_image", _do_print, cut=cut, feed=feed, track=True)

    async def feed(self, hass: HomeAssistant, *, lines: int) -> None:
        try:
            lines_int = int(lines)
        except Exception:
            lines_int = 1
        lines_int = max(lines_int, 1)
        lines_int = min(lines_int, MAX_FEED_LINES)
        _LOGGER.debug("Feeding %s lines", lines_int)

        def _feed_inner(printer: Any) -> None:
            if hasattr(printer, "control"):
                try:
                    for _ in range(lines_int):
                        printer.control("LF")
                except Exception:
                    pass  # Fall through to other methods
                else:
                    return
            if hasattr(printer, "ln"):
                printer.ln(lines_int)
            else:
                try:
                    printer._raw(b"\n" * lines_int)
                except Exception:
                    for _ in range(lines_int):
                        printer.text("\n")

        await self._run_job(hass, "feed", _feed_inner, apply_cut_feed=False)

    async def cut(self, hass: HomeAssistant, *, mode: str) -> None:
        cut_mode = self._map_cut(mode)
        if not cut_mode:
            _LOGGER.warning("Invalid cut mode '%s', defaulting to full", mode)
            cut_mode = "FULL"

        def _cut_inner(printer: Any) -> None:
            printer.cut(mode=cut_mode)

        await self._run_job(hass, "cut", _cut_inner, apply_cut_feed=False)

    async def print_barcode(
        self,
        hass: HomeAssistant,
        *,
        code: str,
        bc: str,
        height: int = 64,
        width: int = 3,
        pos: str = "BELOW",
        font: str = "A",
        align_ct: bool = True,
        check: bool = True,
        force_software: object | None = None,
        align: str | None = None,
        cut: str | None = DEFAULT_CUT,
        feed: int | None = 0,
    ) -> None:
        v_code, v_bc = validate_barcode_data(code, bc)
        height_v = validate_numeric_input(height, 1, 255, "height")
        width_v = validate_numeric_input(width, 2, 6, "width")
        pos_v = (pos or "BELOW").upper()
        if pos_v not in ("ABOVE", "BELOW", "BOTH", "OFF"):
            pos_v = "BELOW"
        font_v = (font or "A").upper()
        if font_v not in ("A", "B"):
            font_v = "A"
        align_m = self._map_align(align)

        def _do_print(printer: Any) -> None:
            if hasattr(printer, "set"):
                # invert=False: undo a white-on-black left by an earlier job
                printer.set(align=align_m, invert=False)
            # Attempt to pass 'force_software' when provided; fall back if unsupported
            kwargs = {
                "height": height_v,
                "width": width_v,
                "pos": pos_v,
                "font": font_v,
                "align_ct": bool(align_ct),
                "check": bool(check),
            }
            if force_software is not None:
                kwargs["force_software"] = force_software

            try:
                printer.barcode(
                    v_code,
                    v_bc,
                    **kwargs,
                )
            except TypeError as e:
                # Older python-escpos may not accept force_software; retry without it
                if "force_software" in kwargs:
                    _LOGGER.debug("force_software unsupported; retrying without it: %s", sanitize_log_message(str(e)))
                    kwargs.pop("force_software", None)
                    printer.barcode(
                        v_code,
                        v_bc,
                        **kwargs,
                    )
                else:
                    raise

        await self._run_job(hass, "print_barcode", _do_print, cut=cut, feed=feed, track=True)

    async def beep(self, hass: HomeAssistant, *, times: int = 2, duration: int = 4) -> None:
        times_v = validate_numeric_input(times, 1, MAX_BEEP_TIMES, "times")
        duration_v = validate_numeric_input(duration, 1, MAX_BEEP_TIMES, "duration")

        def _beep_inner(printer: Any) -> None:
            _LOGGER.debug("beep begin: times=%s duration=%s", times_v, duration_v)
            try:
                if hasattr(printer, "buzzer"):
                    printer.buzzer(times_v, duration_v)
                elif hasattr(printer, "beep"):
                    printer.beep(times_v, duration_v)
                else:
                    _LOGGER.warning("Printer does not support buzzer")
            except AttributeError:
                _LOGGER.warning("Printer does not support buzzer")

        await self._run_job(hass, "beep", _beep_inner, apply_cut_feed=False)

    async def print_sample(self, hass: HomeAssistant, *, title: str, cut: str | None = "full") -> None:
        """Print a test receipt exercising the main text options, as one CUPS job.

        Includes a digit ruler at the configured line width, so a wrong width
        setting shows up as a wrapped or short ruler line.
        """
        cols = max(int(self._config.line_width or 0), 16)
        rule = "=" * cols
        ruler = ("1234567890" * (cols // 10 + 1))[:cols]

        def _build(printer: Any) -> None:
            def style(**kwargs: Any) -> None:
                if hasattr(printer, "set"):
                    printer.set(**kwargs)

            # Explicit baseline: earlier jobs may have left styles behind.
            style(
                align="center", bold=True, underline=0, invert=False,
                custom_size=False, normal_textsize=True,
            )
            if hasattr(printer, "set"):
                _apply_font(printer, "a")
            printer.text(f"{rule}\nESC/POS SAMPLE PRINT\n")
            style(bold=False)
            printer.text(f"{title[:cols]}\n{rule}\n")
            style(align="left")
            printer.text("Normal text\n")
            style(bold=True)
            printer.text("Bold text\n")
            style(bold=False, underline=1)
            printer.text("Underlined text\n")
            style(underline=0, invert=True)
            printer.text(" Inverted text \n")
            style(invert=False, custom_size=True, normal_textsize=False, width=2, height=2)
            printer.text("Double size\n")
            style(custom_size=False, normal_textsize=True)
            if hasattr(printer, "set") and _apply_font(printer, "b") == "b":
                printer.text("Font B (smaller)\n")
                _apply_font(printer, "a")
            printer.text(f"Line width: {cols} columns\n{ruler}\n{rule}\n")
            style(align="center")
            printer.qr(SAMPLE_QR_URL, size=4)
            printer.text("Docs & source:\n")
            printer.text(self._wrap_text(SAMPLE_QR_URL, cols) + "\n")
            style(align="left")

        await self._run_job(hass, "print_sample", _build, cut=cut, feed=2, track=True)

"""Tests for the invert / density / font text styling options."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
from PIL import Image
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.escpos_printer.const import CONF_PRINTER_NAME, DENSITY_LEVELS, DOMAIN
from custom_components.escpos_printer.printer import (
    EscposPrinterAdapter,
    PrinterConfig,
    parse_density,
)


class HassStub:
    async def async_add_executor_job(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _adapter(fake: Any, line_width: int = 48) -> EscposPrinterAdapter:
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P", line_width=line_width))
    adapter._connect = lambda: fake  # type: ignore[method-assign]
    adapter._submit_job = AsyncMock(return_value=1)  # type: ignore[method-assign]
    return adapter


def _set_kwargs(fake: MagicMock) -> list[dict[str, Any]]:
    return [c.kwargs for c in fake.set.call_args_list]


def test_density_levels_are_ordered_lightest_to_darkest() -> None:
    # python-escpos's own 0-8 index is not monotonic; ours must be.
    pcts = [float(k) for k in DENSITY_LEVELS]
    assert pcts == sorted(pcts)
    assert list(DENSITY_LEVELS.values()) == [0, 1, 2, 3, 4, 8, 7, 6, 5]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("+50", 5), ("50", 5), (50, 5), ("+25%", 7), ("-12.5", 3), (0, 4)],
)
def test_parse_density(value: Any, expected: int | None) -> None:
    assert parse_density(value) == expected


@pytest.mark.parametrize("value", ["9", "darkest", 100, "+20"])
def test_parse_density_rejects_unknown(value: Any) -> None:
    with pytest.raises(ValueError, match="density must be one of"):
        parse_density(value)


async def test_print_text_sends_invert_density_and_font() -> None:
    fake = MagicMock()
    await _adapter(fake).print_text(
        HassStub(), text="Hi", invert=True, density="+50", font="B"
    )
    style, font_call = _set_kwargs(fake)
    assert style["invert"] is True
    assert style["density"] == 5
    assert font_call == {"font": "b"}


async def test_print_text_defaults_reset_invert_and_font() -> None:
    """Unstyled text still sends invert=False / font A so earlier state can't leak."""
    fake = MagicMock()
    await _adapter(fake).print_text(HassStub(), text="Hi")
    style, font_call = _set_kwargs(fake)
    assert style["invert"] is False
    assert style["density"] is None  # unchanged darkness
    assert font_call == {"font": "a"}


async def test_print_text_rejects_bad_density_before_printing() -> None:
    fake = MagicMock()
    adapter = _adapter(fake)
    with pytest.raises(ValueError):
        await adapter.print_text(HassStub(), text="Hi", density="loud")
    adapter._submit_job.assert_not_called()  # type: ignore[attr-defined]


async def test_font_b_widens_wrap_using_profile_ratio() -> None:
    fake = MagicMock()
    fake.profile.get_columns.side_effect = lambda f: {"a": 42, "b": 56}[f]
    text = "x" * 60
    await _adapter(fake, line_width=48).print_text(HassStub(), text=text, font="b")
    # 48 * 56 // 42 == 64 columns: 60 chars fit on one line
    assert fake.text.call_args[0][0] == text


async def test_font_a_wraps_at_configured_width() -> None:
    fake = MagicMock()
    await _adapter(fake, line_width=48).print_text(HassStub(), text="x" * 60)
    assert fake.text.call_args[0][0] == "x" * 48 + "\n" + "x" * 12


async def test_unsupported_font_b_falls_back_to_a_and_wraps_at_a_width() -> None:
    fake = MagicMock()

    def _set(**kwargs: Any) -> None:
        if kwargs.get("font") == "b":
            raise RuntimeError("NotSupported: font 1")

    fake.set.side_effect = _set
    fake.profile.get_columns.side_effect = lambda f: {"a": 42, "b": 56}[f]
    await _adapter(fake, line_width=48).print_text(HassStub(), text="x" * 60, font="b")
    assert _set_kwargs(fake)[-1] == {"font": "a"}
    assert fake.text.call_args[0][0] == "x" * 48 + "\n" + "x" * 12


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("print_qr", {"data": "x"}),
        ("print_barcode", {"code": "123456789012", "bc": "EAN13"}),
    ],
)
async def test_non_text_prints_clear_invert(method: str, kwargs: dict[str, Any]) -> None:
    fake = MagicMock()
    await getattr(_adapter(fake), method)(HassStub(), **kwargs)
    assert _set_kwargs(fake)[0]["invert"] is False


async def test_print_image_clears_invert(tmp_path: Any) -> None:
    path = tmp_path / "img.png"
    Image.new("1", (8, 8)).save(path)
    fake = MagicMock()
    with patch(
        "custom_components.escpos_printer.printer.validate_local_image_path",
        return_value=str(path),
    ):
        await _adapter(fake).print_image(HassStub(), image=str(path))
    assert _set_kwargs(fake)[0]["invert"] is False


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


@pytest.mark.parametrize("service", ["print_text", "print_text_utf8"])
async def test_text_services_pass_styling(hass, service: str) -> None:  # type: ignore[no-untyped-def]
    entry = await _setup_entry(hass)
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    with patch.object(adapter, "print_text", AsyncMock()) as mock_print:
        await hass.services.async_call(
            DOMAIN,
            service,
            {"text": "Hi", "invert": True, "density": "-25", "font": "b"},
            blocking=True,
        )
    kw = mock_print.call_args.kwargs
    assert (kw["invert"], kw["density"], kw["font"]) == (True, "-25", "b")


async def test_text_service_bad_density_raises(hass):  # type: ignore[no-untyped-def]
    await _setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="density must be one of"):
        await hass.services.async_call(
            DOMAIN, "print_text", {"text": "Hi", "density": "max"}, blocking=True
        )


async def test_print_message_passes_styling_and_validates_density(hass):  # type: ignore[no-untyped-def]
    entry = await _setup_entry(hass)
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    entity_id = next(s.entity_id for s in hass.states.async_all("notify"))
    with patch.object(adapter, "print_text", AsyncMock()) as mock_print:
        await hass.services.async_call(
            DOMAIN,
            "print_message",
            {"entity_id": entity_id, "message": "Hi", "invert": True, "density": "+12.5", "font": "B"},
            blocking=True,
        )
    kw = mock_print.call_args.kwargs
    assert (kw["invert"], kw["density"], kw["font"]) == (True, "+12.5", "b")

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "print_message",
            {"entity_id": entity_id, "message": "Hi", "density": "+20"},
            blocking=True,
        )

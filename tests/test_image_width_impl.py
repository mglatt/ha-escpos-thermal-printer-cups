"""Tests for the per-entry image width override and impl selection."""

from unittest.mock import MagicMock, patch

from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.escpos_printer import capabilities
from custom_components.escpos_printer import printer as printer_mod
from custom_components.escpos_printer.capabilities import (
    pick_impl,
    profile_declares_no_images,
)
from custom_components.escpos_printer.const import (
    CONF_IMPL,
    CONF_PRINTER_NAME,
    CONF_WIDTH_PIXELS,
    DOMAIN,
)
from custom_components.escpos_printer.printer import (
    EscposPrinterAdapter,
    PrinterConfig,
    _resize_if_wide,
    _supported_image_kwargs,
)


def _registry_with(profile_key: str, **overrides):  # type: ignore[no-untyped-def]
    profile = {
        "name": profile_key,
        "vendor": "Test",
        "codePages": {},
        "fonts": {},
        "features": {},
        "media": {},
    }
    profile.update(overrides)
    return {"profiles": {profile_key: profile}, "encodings": {}}


# ---------------------------------------------------------------------------
# capability helpers
# ---------------------------------------------------------------------------


def test_pick_impl_prefers_raster(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("P", features={"bitImageRaster": True, "bitImageColumn": True}),
    )
    assert pick_impl("P") == "bitImageRaster"


def test_pick_impl_falls_back_to_column_never_graphics(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("P", features={"bitImageColumn": True, "graphics": True}),
    )
    assert pick_impl("P") == "bitImageColumn"
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("P", features={"graphics": True}),
    )
    assert pick_impl("P") is None


def test_pick_impl_auto_profile_is_none() -> None:
    assert pick_impl("") is None
    assert pick_impl(None) is None


def test_profile_declares_no_images(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("P", features={"paperFullCut": True}),
    )
    assert profile_declares_no_images("P") is True
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("P", features={"bitImageRaster": True}),
    )
    assert profile_declares_no_images("P") is False
    # No feature data (auto/unknown): assume capable
    assert profile_declares_no_images("") is False
    assert profile_declares_no_images("unknown-profile") is False


def test_get_profile_pixel_width(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("P", media={"dpi": 203, "width": {"mm": 72, "pixels": 576}}),
    )
    assert capabilities.get_profile_pixel_width("P") == 576
    monkeypatch.setattr(
        capabilities, "_get_capabilities", lambda: _registry_with("P", media={})
    )
    assert capabilities.get_profile_pixel_width("P") is None
    assert capabilities.get_profile_pixel_width("") is None


# ---------------------------------------------------------------------------
# adapter width resolution + resize
# ---------------------------------------------------------------------------


def test_image_target_width_resolution_chain(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 1. Entry override wins
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P", width_pixels=384))
    assert adapter.image_target_width() == 384

    # 2. Profile width when no override
    monkeypatch.setattr(
        capabilities,
        "_get_capabilities",
        lambda: _registry_with("MyProfile", media={"width": {"pixels": 576}}),
    )
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P", profile="MyProfile"))
    assert adapter.image_target_width() == 576

    # 3. Fallback (pre-existing 512 behavior)
    adapter = EscposPrinterAdapter(PrinterConfig(printer_name="P"))
    assert adapter.image_target_width() == 512


def test_resize_respects_target_width() -> None:
    img = Image.new("RGB", (700, 100))
    out = _resize_if_wide(img, 576)
    assert out.width == 576
    # Narrow images pass through untouched
    small = Image.new("RGB", (300, 100))
    assert _resize_if_wide(small, 576) is small


# ---------------------------------------------------------------------------
# impl plumbing into printer.image()
# ---------------------------------------------------------------------------


class _FakePrinterExplicit:
    """image() with a fixed signature and no **kwargs."""

    output = b"escpos-bytes"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    def image(self, img, high_density_vertical=True, high_density_horizontal=True) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "high_density_vertical": high_density_vertical,
                "high_density_horizontal": high_density_horizontal,
            }
        )


class _FakePrinterKwargs:
    """image() with **kwargs — everything must be passed through."""

    output = b"escpos-bytes"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    def image(self, img, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)


def test_supported_image_kwargs_probe() -> None:
    explicit = _FakePrinterExplicit()
    supported = _supported_image_kwargs(explicit)
    assert supported is not None
    assert "impl" not in supported
    assert "high_density_vertical" in supported

    var_kw = _FakePrinterKwargs()
    assert _supported_image_kwargs(var_kw) is None

    # Cached per class
    assert _supported_image_kwargs(_FakePrinterExplicit()) is supported


async def _print_one_image(hass, fake, entry_impl=None, service_impl=None):  # type: ignore[no-untyped-def]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ImgPrinter",
        data={CONF_PRINTER_NAME: "ImgPrinter"},
        unique_id="cups_ImgPrinter",
    )
    entry.add_to_hass(hass)
    with patch("escpos.printer.Dummy"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    if entry_impl is not None:
        adapter.config.impl = entry_impl

    import io

    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="PNG")

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(buf.getvalue())
        path = f.name

    with patch("escpos.printer.Dummy", return_value=fake):
        await adapter.print_image(hass, image=path, impl=service_impl)
    return fake


async def test_impl_dropped_for_printers_without_impl_kwarg(hass):  # type: ignore[no-untyped-def]
    fake = await _print_one_image(hass, _FakePrinterExplicit(), entry_impl="bitImageColumn")
    assert fake.calls, "image() not called"
    assert "impl" not in fake.calls[0]


async def test_entry_impl_passed_through_kwargs_printer(hass):  # type: ignore[no-untyped-def]
    fake = await _print_one_image(hass, _FakePrinterKwargs(), entry_impl="bitImageColumn")
    assert fake.calls[0]["impl"] == "bitImageColumn"


async def test_service_impl_overrides_entry_impl(hass):  # type: ignore[no-untyped-def]
    fake = await _print_one_image(
        hass, _FakePrinterKwargs(), entry_impl="bitImageColumn", service_impl="graphics"
    )
    assert fake.calls[0]["impl"] == "graphics"


async def test_no_impl_omitted_entirely(hass):  # type: ignore[no-untyped-def]
    fake = await _print_one_image(hass, _FakePrinterKwargs())
    assert "impl" not in fake.calls[0]


# ---------------------------------------------------------------------------
# setup-entry resolution and options flow clearing
# ---------------------------------------------------------------------------


async def test_setup_entry_resolves_width_and_impl(hass):  # type: ignore[no-untyped-def]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Cfg",
        data={CONF_PRINTER_NAME: "Cfg", CONF_WIDTH_PIXELS: 576, CONF_IMPL: "bitImageColumn"},
        unique_id="cups_Cfg",
    )
    entry.add_to_hass(hass)
    with patch("escpos.printer.Dummy"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    assert adapter.config.width_pixels == 576
    assert adapter.config.impl == "bitImageColumn"


async def test_options_none_width_clears_data_value(hass):  # type: ignore[no-untyped-def]
    """An explicit None stored by the options flow must beat entry.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Clr",
        data={CONF_PRINTER_NAME: "Clr", CONF_WIDTH_PIXELS: 576},
        options={CONF_WIDTH_PIXELS: None},
        unique_id="cups_Clr",
    )
    entry.add_to_hass(hass)
    with patch("escpos.printer.Dummy"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    assert adapter.config.width_pixels is None
    assert adapter.image_target_width() == 512


async def test_no_image_support_warns_once(hass, caplog, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        printer_mod,
        "_supported_image_kwargs",
        lambda p: None,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Warn",
        data={CONF_PRINTER_NAME: "Warn"},
        unique_id="cups_Warn",
    )
    entry.add_to_hass(hass)
    with patch("escpos.printer.Dummy"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]

    monkeypatch.setattr(
        printer_mod.EscposPrinterAdapter,
        "image_target_width",
        lambda self: 512,
    )
    from custom_components.escpos_printer import capabilities as caps

    monkeypatch.setattr(caps, "profile_declares_no_images", lambda key: True)

    import io as _io
    import tempfile

    buf = _io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="PNG")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(buf.getvalue())
        path = f.name

    fake = MagicMock()
    with patch("escpos.printer.Dummy", return_value=fake):
        await adapter.print_image(hass, image=path)
        await adapter.print_image(hass, image=path)

    warnings = [r for r in caplog.records if "declares no image support" in r.message]
    assert len(warnings) == 1

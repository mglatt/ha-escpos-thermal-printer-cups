"""Tests for device automation actions."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.escpos_printer import device_action
from custom_components.escpos_printer.const import CONF_PRINTER_NAME, DOMAIN


async def _setup_entry_with_device(hass):  # type: ignore[no-untyped-def]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TestPrinter",
        data={CONF_PRINTER_NAME: "TestPrinter"},
        unique_id="cups_TestPrinter",
    )
    entry.add_to_hass(hass)
    with patch("escpos.printer.Dummy"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    return entry, device


async def test_get_actions_lists_all_types(hass):  # type: ignore[no-untyped-def]
    _entry, device = await _setup_entry_with_device(hass)
    actions = await device_action.async_get_actions(hass, device.id)
    assert {a[CONF_TYPE] for a in actions} == device_action.ACTION_TYPES
    assert all(a[CONF_DEVICE_ID] == device.id for a in actions)


async def test_get_actions_unknown_device(hass):  # type: ignore[no-untyped-def]
    assert await device_action.async_get_actions(hass, "no-such-device") == []


async def test_call_print_text_action(hass):  # type: ignore[no-untyped-def]
    _entry, device = await _setup_entry_with_device(hass)
    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device.id,
            CONF_TYPE: "print_text",
            "text": "hello",
        },
        {},
        None,
    )


async def test_call_print_text_utf8_action(hass):  # type: ignore[no-untyped-def]
    _entry, device = await _setup_entry_with_device(hass)
    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device.id,
            CONF_TYPE: "print_text_utf8",
            "text": "curly “quotes”",
        },
        {},
        None,
    )


@pytest.mark.parametrize("action", ["print_text", "print_text_utf8"])
async def test_text_actions_pass_styling(hass, action):  # type: ignore[no-untyped-def]
    entry, device = await _setup_entry_with_device(hass)
    adapter = hass.data[DOMAIN][entry.entry_id]["adapter"]
    config = {
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: device.id,
        CONF_TYPE: action,
        "text": "hello",
        "invert": True,
        "density": "+25",
        "font": "b",
    }
    assert device_action.ACTION_SCHEMA(config)
    with patch.object(adapter, "print_text", AsyncMock()) as mock_print:
        await device_action.async_call_action_from_config(hass, config, {}, None)
    kw = mock_print.call_args.kwargs
    assert (kw["invert"], kw["density"], kw["font"]) == (True, "+25", "b")


async def test_call_qr_feed_cut_beep_actions(hass):  # type: ignore[no-untyped-def]
    _entry, device = await _setup_entry_with_device(hass)
    base = {CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device.id}
    await device_action.async_call_action_from_config(
        hass, {**base, CONF_TYPE: "print_qr", "data": "payload"}, {}, None
    )
    await device_action.async_call_action_from_config(
        hass, {**base, CONF_TYPE: "feed", "lines": 2}, {}, None
    )
    await device_action.async_call_action_from_config(
        hass, {**base, CONF_TYPE: "cut", "mode": "full"}, {}, None
    )
    fake = MagicMock()
    with patch("escpos.printer.Dummy", return_value=fake):
        await device_action.async_call_action_from_config(
            hass, {**base, CONF_TYPE: "beep", "times": 1, "duration": 2}, {}, None
        )
    fake.buzzer.assert_called()


async def test_call_barcode_action(hass):  # type: ignore[no-untyped-def]
    _entry, device = await _setup_entry_with_device(hass)
    fake = MagicMock()
    with patch("escpos.printer.Dummy", return_value=fake):
        await device_action.async_call_action_from_config(
            hass,
            {
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device.id,
                CONF_TYPE: "print_barcode",
                "code": "123456789012",
                "bc": "EAN13",
            },
            {},
            None,
        )
    fake.barcode.assert_called()


async def test_call_action_unknown_device_raises(hass):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="not found"):
        await device_action.async_call_action_from_config(
            hass,
            {
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: "missing",
                CONF_TYPE: "feed",
                "lines": 1,
            },
            {},
            None,
        )


async def test_action_capabilities_have_extra_fields(hass):  # type: ignore[no-untyped-def]
    for action_type in device_action.ACTION_TYPES:
        caps = await device_action.async_get_action_capabilities(
            hass, {CONF_TYPE: action_type}
        )
        assert "extra_fields" in caps

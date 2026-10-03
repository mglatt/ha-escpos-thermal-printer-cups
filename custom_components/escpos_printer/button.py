"""Button platform: Feed, Cut, Beep and Sample print on the device page."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .entity import printer_device_info

_LOGGER = logging.getLogger(__name__)

# One press = one CUPS job; don't queue several at once.
PARALLEL_UPDATES = 1

# Fixed tear-off advance for the Feed button
FEED_LINES = 3


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:  # type: ignore[no-untyped-def]
    async_add_entities(
        [
            EscposFeedButton(hass, entry),
            EscposCutButton(hass, entry),
            EscposBeepButton(hass, entry),
            EscposSamplePrintButton(hass, entry),
        ]
    )


def _button_cut_mode(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """The entry's default cut mode, except "none" becomes "full".

    A Cut button (or a sample receipt left dangling) that does nothing
    reads as broken.
    """
    mode = hass.data[DOMAIN][entry.entry_id]["defaults"].get("cut") or "full"
    return "full" if mode == "none" else mode


class _EscposButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"

    @property
    def device_info(self) -> DeviceInfo:
        return printer_device_info(self._entry)

    @property
    def _adapter(self) -> Any:
        return self.hass.data[DOMAIN][self._entry.entry_id]["adapter"]

    async def async_press(self) -> None:
        try:
            await self._press()
        except HomeAssistantError:
            raise
        except Exception as err:
            # Same contract as the service handlers: full traceback in the
            # log, a HomeAssistantError for the frontend.
            _LOGGER.exception(
                "Button %s failed for entry %s", self._attr_translation_key, self._entry.entry_id
            )
            raise HomeAssistantError(str(err)) from err

    async def _press(self) -> None:
        raise NotImplementedError


class EscposFeedButton(_EscposButton):
    _attr_translation_key = "feed"

    async def _press(self) -> None:
        await self._adapter.feed(self.hass, lines=FEED_LINES)


class EscposCutButton(_EscposButton):
    _attr_translation_key = "cut"

    async def _press(self) -> None:
        await self._adapter.cut(self.hass, mode=_button_cut_mode(self.hass, self._entry))


class EscposBeepButton(_EscposButton):
    _attr_translation_key = "beep"

    async def _press(self) -> None:
        await self._adapter.beep(self.hass)


class EscposSamplePrintButton(_EscposButton):
    _attr_translation_key = "sample_print"

    async def _press(self) -> None:
        await self._adapter.print_sample(
            self.hass, title=self._entry.title, cut=_button_cut_mode(self.hass, self._entry)
        )

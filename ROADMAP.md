# Roadmap

## Next release: standard target picker for print services

**Status:** planned, deferred from the v1.2.0 port (the former upstream
shipped it in its v1.2.0; see `cognitivegears/ha-escpos-thermal-printer`
commit `d919a5f`, "Add HA action targets to print services", and its
`services/target_resolution.py`).

**Goal:** every printing service (`print_text`, `print_text_utf8`,
`print_qr`, `print_image`, `print_barcode`, `feed`, `cut`, `beep`) accepts
Home Assistant's standard `target:` picker, so a call can pick printers
by device, entity, area, floor or label.

**Where things stand today:**

- `services.py::_async_get_target_entries` reads only `call.data["device_id"]`.
  `target: {device_id: ...}` works because HA merges target keys into the
  service data; `area_id`, `floor_id`, `label_id` and `entity_id` are
  silently ignored.
- An empty `device_id` broadcasts to **every** loaded printer. Combined
  with the point above, a call targeted at an area or entity currently
  prints on all printers.
- The docs already promise area/entity targeting
  (`docs/CONFIGURATION.md` "Targeting Printers", `docs/EXAMPLES.md`
  "Target by Area" / "Target by Entity"). Fix those docs if this slips.

**Plan:**

1. `services.yaml`: replace the `device_id` field with a `target:` block
   (device filter `integration: escpos_printer`, entity filter on the
   same integration). Each printer device already carries notify,
   binary_sensor, sensor and button entities, so any of them resolves to
   its printer.
2. Resolve targets with `homeassistant.helpers.service.async_extract_config_entry_ids`
   (or `async_extract_referenced_entity_ids` + device registry), filtered
   to loaded `escpos_printer` entries; keep reading a bare `device_id` in
   data for existing automations.
3. Deprecate untargeted calls: log a warning (or file a Repairs issue)
   when a call has no target and more than one printer is loaded; remove
   broadcast in a later major release. Upstream did the same (deprecated
   in 1.2.0, removal planned for 2.0.0).
4. Tests: area, floor, label and entity targeting; a target that matches
   no printer raises `ServiceValidationError` instead of broadcasting;
   legacy `device_id` still works.
5. Update `strings.json` / `translations/en.json`, README and docs.

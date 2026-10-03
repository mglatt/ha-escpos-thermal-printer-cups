# Notes for Claude Code

CUPS-only fork of an ESC/POS Home Assistant integration
(`custom_components/escpos_printer`). It no longer tracks the former
upstream (`cognitivegears/ha-escpos-thermal-printer`); features are
ported by hand.

## Planned work

- **Next release: the standard target picker (entity/area/floor/label)
  for print services.** See `ROADMAP.md` for the plan. Note that the
  current docs already describe area/entity targeting that the code
  ignores, which today broadcasts to every printer.

## Gotchas

- Jobs are raw ESC/POS submitted to CUPS over IPP with no ESC @ reset,
  so printer style state (invert, font, ...) persists between jobs.
  Send styles explicitly.
- `pyipp`'s `IPP.execute()` returns a plain dict; `"jobs"` and
  `"printers"` are lists of attribute dicts. Keep the unit-test fake in
  `tests/conftest.py` in that shape.
- Pillow ships with Home Assistant core: keep it out of `manifest.json`
  (hassfest rejects it); it stays pinned in `pyproject.toml` for dev/CI.

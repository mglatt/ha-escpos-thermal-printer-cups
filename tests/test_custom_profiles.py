"""Tests for integration-registered custom profiles and registry patches."""

from typing import Any

from custom_components.escpos_printer.custom_profiles import (
    _dedupe_clone_codepages,
    _patch_nt80vul_fonts,
    _register_rp820,
    _register_tm_m10,
    _register_tm_m30iii,
    register_custom_profiles,
)


def _tm_t20ii() -> dict[str, Any]:
    return {
        "name": "TM-T20II",
        "vendor": "Epson",
        "media": {"dpi": 203, "width": {"mm": 72, "pixels": 576}},
        "features": {"graphics": True, "bitImageRaster": True, "paperFullCut": True},
        "colors": {"black": True},
        "fonts": {
            "0": {"name": "Font A", "columns": 48},
            "1": {"name": "Font B", "columns": 64},
        },
        "codePages": {"0": "CP437", "2": "CP850", "16": "CP1252", "19": "CP858"},
    }


def _default() -> dict[str, Any]:
    return {
        "name": "Default",
        "vendor": "Generic",
        "features": {"bitImageRaster": True},
        "colors": {"black": True},
        "codePages": {"0": "CP437"},
        "fonts": {"0": {"name": "Font A", "columns": 48}},
    }


# ---------------------------------------------------------------------------
# codepage dedup
# ---------------------------------------------------------------------------


def test_dedupe_drops_high_duplicate_only_when_low_exists() -> None:
    profiles = {
        "P": {
            "codePages": {
                "0": "CP437",
                "52": "CP437",  # >=48 duplicate of a <48 index — dropped
                "50": "CP1250",  # only index for its name — kept
                "9": "CP858",
                "16": "CP858",  # both <48 — untouched (not lowest-wins)
            }
        }
    }
    _dedupe_clone_codepages(profiles)
    cp = profiles["P"]["codePages"]
    assert "52" not in cp
    assert cp["0"] == "CP437"
    assert cp["50"] == "CP1250"
    assert cp["9"] == "CP858"
    assert cp["16"] == "CP858"


def test_dedupe_leaves_unknown_placeholders_alone() -> None:
    profiles = {"P": {"codePages": {"11": "Unknown", "51": "Unknown"}}}
    _dedupe_clone_codepages(profiles)
    assert profiles["P"]["codePages"] == {"11": "Unknown", "51": "Unknown"}


# ---------------------------------------------------------------------------
# NT-80-V-UL font patch
# ---------------------------------------------------------------------------


def test_patch_nt80vul_fonts_corrects_and_is_idempotent() -> None:
    profiles = {
        "NT-80-V-UL": {
            "fonts": {"0": {"columns": 12}, "1": {"columns": 9}},
        }
    }
    _patch_nt80vul_fonts(profiles)
    assert profiles["NT-80-V-UL"]["fonts"]["0"]["columns"] == 48
    assert profiles["NT-80-V-UL"]["fonts"]["1"]["columns"] == 64
    _patch_nt80vul_fonts(profiles)  # second call is a no-op
    assert profiles["NT-80-V-UL"]["fonts"]["0"]["columns"] == 48


def test_patch_nt80vul_missing_profile_is_noop() -> None:
    _patch_nt80vul_fonts({})  # must not raise


# ---------------------------------------------------------------------------
# RP820
# ---------------------------------------------------------------------------


def test_register_rp820_overrides_template() -> None:
    profiles = {"TM-T20II": _tm_t20ii()}
    _register_rp820(profiles)
    rp820 = profiles["RP820"]
    assert rp820["vendor"] == "Rongta"
    assert rp820["media"]["width"]["pixels"] == 576
    assert rp820["features"]["graphics"] is False
    assert rp820["fonts"]["0"]["columns"] == 48
    assert rp820["fonts"]["1"]["columns"] == 64
    # Deep copy: template untouched
    assert profiles["TM-T20II"]["features"]["graphics"] is True
    # Same codepage table as the template
    assert rp820["codePages"] == profiles["TM-T20II"]["codePages"]


def test_register_rp820_idempotent_does_not_rebind() -> None:
    profiles = {"TM-T20II": _tm_t20ii()}
    _register_rp820(profiles)
    first = profiles["RP820"]
    _register_rp820(profiles)
    assert profiles["RP820"] is first


def test_register_rp820_without_template_skips() -> None:
    profiles: dict[str, Any] = {}
    _register_rp820(profiles)
    assert "RP820" not in profiles


# ---------------------------------------------------------------------------
# TM-m30III / TM-m10
# ---------------------------------------------------------------------------


def test_register_tm_m30iii() -> None:
    profiles = {"default": _default()}
    _register_tm_m30iii(profiles)
    m30 = profiles["TM-m30III"]
    assert m30["media"]["width"]["pixels"] == 576
    assert m30["fonts"]["1"]["columns"] == 57
    assert m30["codePages"]["16"] == "CP1252"
    assert m30["codePages"]["19"] == "Unknown"  # deliberately not CP858
    assert m30["features"] is profiles["default"]["features"]


def test_register_tm_m10_borrows_m30iii_codepages() -> None:
    profiles = {"default": _default()}
    _register_tm_m30iii(profiles)
    _register_tm_m10(profiles)
    m10 = profiles["TM-m10"]
    assert m10["media"]["width"]["pixels"] == 420
    assert m10["fonts"]["0"]["columns"] == 35
    assert m10["codePages"] == profiles["TM-m30III"]["codePages"]
    assert m10["codePages"] is not profiles["TM-m30III"]["codePages"]  # own copy


def test_register_tm_m10_requires_m30iii() -> None:
    profiles: dict[str, Any] = {}
    _register_tm_m10(profiles)
    assert "TM-m10" not in profiles


# ---------------------------------------------------------------------------
# top-level entry point
# ---------------------------------------------------------------------------


def test_register_custom_profiles_survives_missing_escpos() -> None:
    """Under the test env's stubbed escpos (no capabilities module) this
    must degrade to a logged warning, never an exception."""
    register_custom_profiles()
    register_custom_profiles()  # idempotent, still no crash

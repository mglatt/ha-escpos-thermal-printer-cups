"""Tests for clone-model profile aliases."""

from typing import Any

from custom_components.escpos_printer import capabilities
from custom_components.escpos_printer.profile_aliases import (
    ALIAS_MODELS,
    PROFILE_ALIASES,
    _alias_keys,
    canonical_profile_key,
    normalize_model,
    resolve_alias,
)


def _registry(*keys: str) -> dict[str, Any]:
    return {
        "profiles": {
            key: {"name": key, "vendor": "Test", "codePages": {}, "fonts": {}} for key in keys
        },
        "encodings": {},
    }


def test_normalize_model() -> None:
    assert normalize_model("CT-S601 II") == "cts601ii"
    assert normalize_model("Epson TM-T88VI") == "epsontmt88vi"
    assert normalize_model("POS_5890-K!") == "pos5890k"


def test_alias_keys_strip_vendor_word() -> None:
    assert _alias_keys("Citizen CT-S601II") == ("citizencts601ii", "cts601ii")
    assert _alias_keys("Standalone") == ("standalone",)


def test_alias_table_contains_both_key_forms() -> None:
    assert PROFILE_ALIASES["epsontmt88vi"] == "TM-T88V"
    assert PROFILE_ALIASES["tmt88vi"] == "TM-T88V"


def test_resolve_alias() -> None:
    assert resolve_alias("Epson TM-T88VI") == "TM-T88V"
    assert resolve_alias("tm-t88vi") == "TM-T88V"
    assert resolve_alias("Totally Unknown Model") is None


def test_every_alias_resolves_against_registered_targets(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With all alias targets registered, every display name and key resolves."""
    monkeypatch.setattr(
        capabilities, "_get_capabilities", lambda: _registry(*set(ALIAS_MODELS.values()))
    )
    for display, target in ALIAS_MODELS.items():
        assert capabilities.resolve_profile_name(display) == target, display
        for key in _alias_keys(display):
            assert capabilities.resolve_profile_name(key) == target, key


def test_resolve_profile_name_exact_and_case_variants(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(capabilities, "_get_capabilities", lambda: _registry("TM-T88V"))
    assert capabilities.resolve_profile_name("TM-T88V") == "TM-T88V"
    assert capabilities.resolve_profile_name("tm-t88v") == "TM-T88V"
    assert capabilities.resolve_profile_name("  TM-T88V  ") == "TM-T88V"
    assert capabilities.resolve_profile_name("nope") is None
    assert capabilities.resolve_profile_name("") is None
    assert capabilities.resolve_profile_name(None) is None


def test_canonical_profile_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(capabilities, "_get_capabilities", lambda: _registry("TM-T88V"))
    assert canonical_profile_key("epsontmt88vii") == "TM-T88V"
    assert canonical_profile_key("TM-T88V") == "TM-T88V"
    # Unresolvable input passes through unchanged
    assert canonical_profile_key("mystery") == "mystery"
    assert canonical_profile_key(None) is None
    assert canonical_profile_key("") == ""


def test_is_valid_profile_accepts_aliases(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(capabilities, "_get_capabilities", lambda: _registry("TM-T88V"))
    assert capabilities.is_valid_profile("epsontmt88vi")
    assert capabilities.is_valid_profile("tm-t88v")
    assert not capabilities.is_valid_profile("mystery")


def test_profile_choices_include_compatible_rows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(capabilities, "_get_capabilities", lambda: _registry("TM-T88V"))
    choices = capabilities.get_profile_choices()
    as_dict = dict(choices)
    assert as_dict["epsontmt88vi"] == "Epson TM-T88VI (compatible)"
    # Combined sort keeps compatible rows near their siblings, before Custom
    labels = [label for _key, label in choices]
    assert labels[-1].startswith("Custom")


def test_profile_choices_skip_alias_colliding_with_real_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # "Epson TM-T20III" normalizes to tmt20iii — a real key here, so no alias row
    monkeypatch.setattr(
        capabilities, "_get_capabilities", lambda: _registry("TM-T20II", "TM-T20III")
    )
    as_dict = dict(capabilities.get_profile_choices())
    assert "epsontmt20iii" not in as_dict
    # Non-colliding aliases still present
    assert "epsontmt88vi" in as_dict


def test_lookup_functions_follow_aliases(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    registry = _registry("TM-T88V")
    registry["profiles"]["TM-T88V"]["codePages"] = {"0": "CP437", "16": "CP1252"}
    registry["profiles"]["TM-T88V"]["fonts"] = {"0": {"name": "Font A", "columns": 42}}
    registry["profiles"]["TM-T88V"]["features"] = {"paperFullCut": True, "qrCode": True}
    monkeypatch.setattr(capabilities, "_get_capabilities", lambda: registry)

    assert capabilities.get_profile_codepages("epsontmt88vi") == sorted(["CP437", "CP1252"])
    assert capabilities.get_profile_line_widths("epsontmt88vi") == [42]
    assert "full" in capabilities.get_profile_cut_modes("epsontmt88vi")
    assert capabilities.profile_supports_feature("epsontmt88vi", "qrCode")
    assert capabilities.get_profile_features("epsontmt88vi")["qrCode"] is True
    assert capabilities.get_profile_info("epsontmt88vi")["name"] == "TM-T88V"

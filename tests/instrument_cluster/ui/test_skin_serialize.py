"""Skin serializer round-trips (the editor's save path).

``emit_skin_module(scale=None)`` must reproduce a skin *verbatim* — a save
that silently rounded or snapped hand-tuned values would corrupt design
work. The scaled path (``tools/gen_skin_seed.py``) must seed every
registered skin, carrying resolution-independent fields over unchanged.
"""

import importlib.util
import sys

import pytest

from instrument_cluster.ui.skins import (
    SKIN_800,
    SKIN_1024,
    SKIN_1280,
    SKIN_1280_CAR3588,
    set_skin_override,
)
from instrument_cluster.ui.skins.serialize import emit_skin_module


def _exec_emitted(text: str, tmp_path, stem: str, var: str):
    module_path = tmp_path / f"{stem}.py"
    module_path.write_text(text)
    spec = importlib.util.spec_from_file_location(f"instrument_cluster.ui.skins.{stem}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return getattr(module, var)
    finally:
        del sys.modules[spec.name]


@pytest.mark.parametrize("skin", [SKIN_1280, SKIN_1280_CAR3588], ids=lambda s: s.name)
@pytest.mark.parametrize("size", [(1024, 600), (800, 480), (1920, 1080)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_seeding_scales_and_keeps_constants(skin, size, tmp_path):
    w, h = size
    fx, fy = w / skin.width, h / skin.height
    text = emit_skin_module(skin, size=size, scale=(fx, fy, min(fx, fy)))
    var = f"SKIN_{w}" + (f"_CAR{skin.car_id}" if skin.car_id is not None else "")
    seeded = _exec_emitted(text, tmp_path, f"skin_seed_{w}_{skin.car_id}", var)

    assert seeded.size == size
    assert seeded.car_id == skin.car_id
    # const strings (a crash once: the scaler compared them with 0)
    assert seeded.dashboard.rpm_variant == skin.dashboard.rpm_variant
    assert seeded.setup.visible_network_cells == skin.setup.visible_network_cells
    assert seeded.dashboard.gear_color == skin.dashboard.gear_color
    # x/y follow their own axis
    x, y, rw, rh = skin.dashboard.gear_rect
    assert seeded.dashboard.gear_rect == (round(x * fx), round(y * fy), round(rw * fx), round(rh * fy))
    # pixel fonts stay on Pixeltype's 8 px grid
    assert seeded.style.header_font_size % 8 == 0


@pytest.mark.parametrize(
    "skin", [SKIN_1280, SKIN_1024, SKIN_800], ids=lambda s: s.name
)
def test_verbatim_round_trip(skin, tmp_path):
    text = emit_skin_module(skin, docstring="round-trip test emission")
    module_path = tmp_path / f"skin_rt_{skin.width}.py"
    module_path.write_text(text)

    # The emitted module does `from .schema import ...`; import it as if it
    # lived inside the skins package.
    spec = importlib.util.spec_from_file_location(
        f"instrument_cluster.ui.skins.skin_rt_{skin.width}", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        rebuilt = getattr(module, f"SKIN_{skin.width}")
    finally:
        del sys.modules[spec.name]

    assert rebuilt == skin


def test_strings_emit_with_double_quotes():
    # Family names must be written "..." like the hand-written skin files —
    # repr()'s single quotes would churn every family line on the first
    # editor save of an otherwise-untouched skin.
    text = emit_skin_module(SKIN_800)
    assert 'gear_family="D_DIN_EXP_BOLD",' in text
    assert 'name="800x480",' in text
    body = text.split("SKIN_", 1)[1]
    assert "'" not in body, "single-quoted string leaked into the emission"


def test_skin_override_hook(force_profile):
    from dataclasses import replace

    from instrument_cluster.ui.skins import active_skin, reset_skin_overrides

    edited = replace(SKIN_800, name="800x480-edited")
    try:
        set_skin_override(edited)
        with force_profile("waveshare_5"):
            assert active_skin() is edited
    finally:
        reset_skin_overrides()
    with force_profile("waveshare_5"):
        assert active_skin() == SKIN_800

"""Serialize a :class:`Skin` back to a ``skin_*.py`` module.

Two consumers share this code path: ``tools/gen_skin_seed.py`` (seed a new
skin by scaling SKIN_1280) and the skin editor (save a hand-tuned skin
verbatim). Scaling follows each schema field's axis metadata; with
``scale=None`` values are emitted **verbatim** — no rounding, no font
snapping — so saving an edited skin can never silently change hand-tuned
values (the round-trip test pins this).
"""

from __future__ import annotations

from dataclasses import is_dataclass

from .schema import Skin, axis_of, iter_px_fields

#: (fx, fy, fu) scale triple; None means verbatim emission.
Scale = tuple[float, float, float]


def snap_even(v: float) -> int:
    return max(2, round(v / 2) * 2)


def snap_pixel_font(v: float) -> int:
    return max(8, round(v / 8) * 8)


def _scale_scalar(value, axis: str, fx: float, fy: float, fu: float):
    if axis in ("family", "color"):
        return value  # FontFamily / Color member name — resolution-independent
    if axis == "const":
        return value  # counts, ratios, and string choices like rpm_variant
    sign = -1 if value < 0 else 1
    mag = abs(value)
    if axis == "x":
        return sign * round(mag * fx)
    if axis == "y":
        return sign * round(mag * fy)
    if axis == "u":
        return sign * max(1 if mag else 0, round(mag * fu))
    if axis == "font":
        return snap_even(mag * fu)
    if axis == "font_pixel":
        return snap_pixel_font(mag * fu)
    raise ValueError(f"scalar field with axis {axis!r}")


def _scale_value(value, axis: str, scale: Scale | None):
    if scale is None:
        return value
    fx, fy, fu = scale
    if axis == "pos" or axis == "size":
        x, y = value
        return (round(x * fx), round(y * fy))
    if axis == "rect":
        x, y, w, h = value
        return (round(x * fx), round(y * fy), round(w * fx), round(h * fy))
    return _scale_scalar(value, axis, fx, fy, fu)


def _emit(
    obj,
    scale: Scale | None,
    tune: frozenset[str],
    indent: int,
    out: list[str],
    head: tuple[str, ...] = (),
) -> None:
    pad = " " * indent
    out.append(f"{type(obj).__name__}(")
    out.extend(f"{pad}    {line}" for line in head)
    for f, value in iter_px_fields(obj):
        axis = axis_of(f)
        if f.name in ("name", "size", "car_id"):
            continue  # emitted via `head` on the top-level Skin
        mark = "  # TODO tune" if f.name in tune else ""
        if axis == "group":
            out.append(f"{pad}    {f.name}=")
            _emit(value, scale, tune, indent + 4, out)
            out[-1] += ","
        else:
            scaled = _scale_value(value, axis, scale)
            out.append(f"{pad}    {f.name}={_format_value(scaled)},{mark}")
    out.append(f"{pad})")


def _format_value(value) -> str:
    """Emit a field value as source text.

    Strings (font-family names) are written with **double quotes** to match
    the hand-written skin files — plain ``repr()`` would emit single quotes
    and churn every family line on the first editor save.
    """
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return repr(value)


def _merge_nested(lines: list[str]) -> str:
    # Join the "field=" line with the constructor line that follows it.
    return "\n".join(lines).replace("=\n", "=")


def _class_names(obj) -> set[str]:
    names = {type(obj).__name__}
    for _, value in iter_px_fields(obj):
        if is_dataclass(value):
            names |= _class_names(value)
    return names


def emit_skin_module(
    skin: Skin,
    *,
    size: tuple[int, int] | None = None,
    scale: Scale | None = None,
    tune: frozenset[str] = frozenset(),
    docstring: str = "",
) -> str:
    """Full ``skin_*.py`` module text for ``skin``.

    ``scale=None`` emits every value verbatim (the editor's save path);
    a ``(fx, fy, fu)`` triple scales per the axis metadata (the seed
    generator). ``size`` overrides the emitted ``Skin.size``/``name``
    (used when seeding a new resolution from SKIN_1280); default is the
    skin's own.
    """
    w, h = size if size is not None else skin.size
    var = f"SKIN_{w}"
    head = [f'name="{w}x{h}",', f"size=({w}, {h}),"]
    # A car skin shares its resolution with the panel default, so both the
    # module symbol and Skin.name have to carry the car too — otherwise the
    # editor's save path emits a second definition of SKIN_<w> and the file
    # silently shadows the base skin.
    if skin.car_id is not None:
        var = f"{var}_CAR{skin.car_id}"
        head[0] = f'name="{w}x{h}-car{skin.car_id}",'
        head.insert(1, f"car_id={skin.car_id},")
    lines: list[str] = []
    _emit(
        skin,
        scale,
        tune,
        0,
        lines,
        head=tuple(head),
    )
    body = _merge_nested(lines)

    parts = []
    if docstring:
        parts.append(f'"""{docstring}"""\n')
    parts.append("from .schema import (")
    for name in sorted(_class_names(skin)):
        parts.append(f"    {name},")
    parts.append(")")
    parts.append("")
    parts.append(f"{var} = {body}")
    return "\n".join(parts) + "\n"

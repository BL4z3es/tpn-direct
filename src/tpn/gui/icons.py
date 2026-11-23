"""Pillow-drawn icons, shared by the tray, the window and the installer icon.

Pillow is deliberately imported lazily: the headless control model must not need it. Drawing
at high resolution and downsampling gives the small tray icon antialiased edges without an
asset pipeline or a platform-specific resource file.
"""

from __future__ import annotations

from typing import Literal

IconKind = Literal["disconnected", "connecting", "direct", "tor", "failed"]
ICON_KINDS: tuple[IconKind, ...] = ("disconnected", "connecting", "direct", "tor", "failed")
ICON_SIZES = (16, 24, 32, 48, 64, 256)

# Muted outlines keep the state colour visible on both light and dark notification areas.
ICON_COLOURS: dict[IconKind, tuple[int, int, int]] = {
    "disconnected": (125, 134, 145),
    "connecting": (229, 151, 25),
    "direct": (33, 173, 91),
    "tor": (50, 126, 218),
    "failed": (218, 65, 72),
}


def render_icon(kind: IconKind, size: int) -> object:
    """Render a shield-and-route icon at `size` pixels; all states have distinct colours."""
    if kind not in ICON_KINDS:
        raise ValueError(f"unknown icon kind: {kind}")
    if not isinstance(size, int) or size < 1:
        raise ValueError("icon size must be a positive integer")
    from PIL import Image, ImageDraw

    scale = 4
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    unit = size * scale / 64.0

    def points(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x * unit, y * unit) for x, y in coords]

    colour = ICON_COLOURS[kind]
    edge = tuple(round(c * 0.67) for c in colour) + (255,)
    fill = colour + (255,)
    width = max(scale, round(4.5 * unit))

    # A rounded shield, rather than a text glyph, remains recognisable at 16 pixels.
    shield = [(10, 7), (31, 3), (53, 7), (54, 31), (48, 45), (32, 60), (16, 45), (10, 31)]
    draw.polygon(points(shield), fill=edge)
    inner = [(14, 10), (31, 7), (49, 10), (50, 30), (44, 42), (32, 54), (20, 42), (14, 30)]
    draw.polygon(points(inner), fill=fill)

    # The ring and its outward route suggest a protected exit-node connection. The white cut-out
    # and short tail are as important as the hue for users who cannot distinguish the colours.
    cx, cy, radius = 31.0 * unit, 28.0 * unit, 11.0 * unit
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(255, 255, 255, 255),
    )
    inner_radius = radius - width
    draw.ellipse(
        (cx - inner_radius, cy - inner_radius, cx + inner_radius, cy + inner_radius), fill=fill
    )
    draw.line(points([(31, 28), (31, 42), (43, 42)]), fill=(255, 255, 255, 255), width=width)
    draw.polygon(
        points([(39, 38), (47, 42), (39, 46)]),
        fill=(255, 255, 255, 255),
    )

    return canvas.resize((size, size), Image.Resampling.LANCZOS)

"""Window grid: fewer clones get bigger windows, never full screen; fill left to right, then the next row."""
from __future__ import annotations

import math

ROBLOX_ASPECT = 16 / 9  # landscape game view


def layout(n: int, width: int, height: int, landscape: bool = True, gap: int = 4) -> list[tuple[int, int, int, int]]:
    """Return n window rectangles (left, top, right, bottom) in screen pixels.

    The screen is treated in the requested orientation. The grid shape is the one whose cells hold the biggest
    16:9 (landscape) or 9:16 (portrait) window, with at least 2 cells so a single clone is never full screen.
    """
    if n <= 0:
        return []
    sw, sh = (max(width, height), min(width, height)) if landscape else (min(width, height), max(width, height))
    aspect = ROBLOX_ASPECT if landscape else 1 / ROBLOX_ASPECT
    slots = max(n, 2)

    best: tuple[tuple[float, int, int], int, int] | None = None
    for cols in range(1, slots + 1):
        rows = math.ceil(slots / cols)
        cw, ch = sw / cols, sh / rows
        # biggest window of the target aspect that fits in a cell
        w = min(cw, ch * aspect)
        area = round(w * (w / aspect))
        # ties: fewer empty cells, then more columns (fill left to right first)
        key = (area, -(cols * rows - slots), cols)
        if best is None or key > best[0]:
            best = (key, cols, rows)
    assert best
    _, cols, rows = best
    cw, ch = sw // cols, sh // rows
    rects = []
    for i in range(n):
        r, c = divmod(i, cols)
        left, top = c * cw, r * ch
        right = sw if c == cols - 1 else left + cw
        bottom = sh if r == rows - 1 else top + ch
        rects.append((left + gap // 2, top + gap // 2, right - gap // 2, bottom - gap // 2))
    return rects

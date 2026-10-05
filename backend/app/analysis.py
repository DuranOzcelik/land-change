"""Tile classification, land-cover statistics and change detection."""
from __future__ import annotations

from collections import Counter

import numpy as np

from .imagery import MASK_SCALE, PIXEL_M, TILE
from .model import CLASSES

GROUPS = {
    "agriculture": ["AnnualCrop", "PermanentCrop", "Pasture"],
    "vegetation": ["Forest", "HerbaceousVegetation"],
    "built": ["Residential", "Industrial", "Highway"],
    "water": ["River", "SeaLake"],
}
GROUP_KEYS = list(GROUPS)
CLASS_TO_GROUP = np.array([next(g for g, m in enumerate(GROUPS.values()) if c in m) for c in CLASSES])
WATER_CLASSES = np.array([CLASSES.index(c) for c in GROUPS["water"]])
LAND_CLASSES = np.setdiff1d(np.arange(len(CLASSES)), WATER_CLASSES)
SEA = CLASSES.index("SeaLake")

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)
CELL_SIZES = (320, 640, 1280)   # metres
BASE_CELL = 640                 # numbers are always computed at the model's native cell size
INVALID_SHARE = 0.2             # a cell with more cloud or missing data than this is left out


def _block_mean(mask: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Mean of a 2-D mask over a rows x cols grid of equal blocks."""
    bh, bw = mask.shape[0] // rows, mask.shape[1] // cols
    m = mask[:rows * bh, :cols * bw].astype(np.float32)
    return m.reshape(rows, bh, cols, bw).mean(axis=(1, 3))


def classify(scene: dict, predict, cell_m: int = 640, batch_rows: int = 8) -> dict:
    """Classify a scene on a grid of `cell_m` cells.

    The network always looks at a 640 m window. 320 m cells come from sliding that window by
    half a tile, 1280 m cells from averaging 2x2 neighbouring predictions.
    """
    rgb = scene["rgb"]
    x = (rgb.transpose(2, 0, 1).astype(np.float32) / 255.0 - MEAN) / STD
    stride = TILE // 2 if cell_m == 320 else TILE
    windows = np.lib.stride_tricks.sliding_window_view(x, (TILE, TILE), axis=(1, 2))[:, ::stride, ::stride]
    rows, cols = windows.shape[1:3]

    probs = np.concatenate([
        predict(np.ascontiguousarray(
            windows[:, r:r + batch_rows].transpose(1, 2, 0, 3, 4)).reshape(-1, 3, TILE, TILE))
        for r in range(0, rows, batch_rows)]).reshape(rows, cols, len(CLASSES))

    inset_px = (TILE - stride) // 2
    cell_px = cell_m // PIXEL_M
    if cell_m == 1280:
        rows, cols = rows // 2, cols // 2
        probs = probs[:rows * 2, :cols * 2].reshape(rows, 2, cols, 2, -1).mean(axis=(1, 3))
    probs = probs.reshape(rows * cols, -1).copy()

    # The scene-classification layer (derived from infrared bands) decides what is water:
    # RGB alone cannot tell dark sea from dark forest.
    lo, hi = inset_px // MASK_SCALE, inset_px // MASK_SCALE
    span_r, span_c = rows * cell_px // MASK_SCALE, cols * cell_px // MASK_SCALE
    def cell_share(name):
        mask = scene.get(name)
        if mask is None:
            return np.zeros(rows * cols, dtype=np.float32)
        return _block_mean(mask[lo:lo + span_r, hi:hi + span_c], rows, cols).reshape(-1)

    water = cell_share("water")
    valid = (cell_share("nodata") <= INVALID_SHARE) & (cell_share("cloud") <= INVALID_SHARE)

    dry, wet = water < 0.1, water >= 0.5
    probs[np.ix_(dry, WATER_CLASSES)] = 0
    probs[np.ix_(wet, LAND_CLASSES)] = 0
    probs[wet & (probs.sum(axis=1) == 0), SEA] = 1
    total = probs.sum(axis=1, keepdims=True)
    probs = np.divide(probs, total, out=np.zeros_like(probs), where=total > 0)

    gprobs = np.zeros((len(probs), len(GROUP_KEYS)), dtype=np.float32)
    np.add.at(gprobs, (slice(None), CLASS_TO_GROUP), probs)
    conf = gprobs.max(axis=1)
    conf[~valid] = 0           # clouds, shadows and gaps are never trusted

    return {"rows": rows, "cols": cols, "cell_m": cell_m, "cell_px": cell_px, "inset_px": inset_px,
            "groups": gprobs.argmax(axis=1).reshape(rows, cols).astype(np.int8),
            "classes": probs.argmax(axis=1).reshape(rows, cols).astype(np.int8),
            "conf": conf.reshape(rows, cols), "valid": valid.reshape(rows, cols)}


def cell_km2(result: dict) -> float:
    return (result["cell_m"] / 1000) ** 2


def stats(result: dict, mask: np.ndarray | None = None) -> dict:
    """Share (%) and area (km2) of every group and class over the usable cells.

    `mask` narrows the cells further, e.g. to those usable on both dates of a comparison.
    """
    mask = result["valid"] if mask is None else mask
    n, area = int(mask.sum()), cell_km2(result)

    def table(grid, keys):
        counts = np.bincount(grid[mask].reshape(-1), minlength=len(keys))
        return [{"key": k, "share": round(100 * int(c) / n, 1) if n else 0.0, "km2": round(int(c) * area, 2)}
                for k, c in zip(keys, counts)]

    low = float((result["conf"][mask] < 0.6).mean()) if n else 0.0
    return {"groups": table(result["groups"], GROUP_KEYS), "classes": table(result["classes"], CLASSES),
            # total_km2 is the area the shares refer to (usable cells); area_km2 is the whole square
            "total_km2": round(n * area, 1), "valid_km2": round(n * area, 1),
            "area_km2": round(result["groups"].size * area, 1),
            "low_confidence_share": round(100 * low, 1)}


def compare(before: dict, after: dict, min_conf: float = 0.8) -> dict:
    """Change between two classifications of the same grid.

    Only cells usable on both dates are considered. `built_km2` counts cells that became built-up
    with a confident prediction on both dates; `net_built_km2` is the plain difference in built-up
    area. A large gap between the two means the model is unsure about this scene.
    """
    ga, gb = before["groups"], after["groups"]
    both = before["valid"] & after["valid"]
    changed = (ga != gb) & (before["conf"] >= min_conf) & (after["conf"] >= min_conf) & both
    area = cell_km2(before)
    moves = Counter(zip(ga[changed].tolist(), gb[changed].tolist())).most_common()
    built = GROUP_KEYS.index("built")
    gained = round(float((changed & (gb == built)).sum()) * area, 2)
    net = round(float(((gb == built) & both).sum() - ((ga == built) & both).sum()) * area, 2)
    return {
        "changed": changed,
        "both_valid": both,
        "changed_km2": round(float(changed.sum()) * area, 2),
        "built_km2": gained,
        "net_built_km2": net,
        "valid_km2": round(float(both.sum()) * area, 1),
        "uncertain": abs(gained - net) > max(2.0, 0.6 * max(gained, abs(net))),
        "transitions": [{"from": GROUP_KEYS[a], "to": GROUP_KEYS[b], "km2": round(n * area, 2)}
                        for (a, b), n in moves],
    }

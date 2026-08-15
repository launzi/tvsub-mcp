from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Calibration:
    scale: float
    offset: float
    warning: str | None = None


def calculate(anchors: list[dict]) -> Calibration:
    ordered = sorted(anchors, key=lambda item: float(item["subTime"]))
    if not ordered:
        return Calibration(1.0, 0.0)
    if len(ordered) == 1:
        return Calibration(1.0, float(ordered[0]["actualTime"]) - float(ordered[0]["subTime"]))
    xs = [float(item["subTime"]) for item in ordered]
    ys = [float(item["actualTime"]) for item in ordered]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(value * value for value in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denominator = n * sxx - sx * sx
    if abs(denominator) < 1e-9:
        scale = 1.0
        offset = ys[0] - xs[0]
    else:
        scale = (n * sxy - sx * sy) / denominator
        offset = (sy - scale * sx) / n
    span = max(xs) - min(xs)
    warning = None
    if span < 300:
        warning = f"앵커 간격이 {span:.0f}초뿐이라 scale 추정이 불안정합니다."
    elif scale < 0.9 or scale > 1.15:
        warning = f"scale={scale:.4f}가 정상 범위(0.9~1.15)를 벗어났습니다."
    return Calibration(scale, offset, warning)


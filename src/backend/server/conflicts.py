from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CorridorMargins:
    horizontal_m: float
    vertical_m: float
    temporal_s: float


def _interpolate(
    start: list[float],
    end: list[float],
    timestamp: float,
) -> tuple[float, float, float]:
    duration = end[3] - start[3]
    if duration <= 0:
        return (start[0], start[1], start[2])
    ratio = max(0.0, min(1.0, (timestamp - start[3]) / duration))
    return (
        start[0] + (end[0] - start[0]) * ratio,
        start[1] + (end[1] - start[1]) * ratio,
        start[2] + (end[2] - start[2]) * ratio,
    )


def _position_at(
    route: list[list[float]],
    timestamp: float,
) -> tuple[float, float, float] | None:
    if not route:
        return None
    if timestamp < route[0][3] or timestamp > route[-1][3]:
        return None
    for start, end in zip(route, route[1:]):
        if start[3] <= timestamp <= end[3]:
            return _interpolate(start, end, timestamp)
    return tuple(route[-1][:3])  # type: ignore[return-value]


def routes_conflict(
    first: list[list[float]],
    second: list[list[float]],
    *,
    first_margins: CorridorMargins,
    second_margins: CorridorMargins,
    sample_interval_s: float,
) -> bool:
    if len(first) < 2 or len(second) < 2:
        return False

    temporal = first_margins.temporal_s + second_margins.temporal_s
    overlap_start = max(first[0][3], second[0][3]) - temporal
    overlap_end = min(first[-1][3], second[-1][3]) + temporal
    if overlap_start > overlap_end:
        return False

    horizontal_limit = first_margins.horizontal_m + second_margins.horizontal_m
    vertical_limit = first_margins.vertical_m + second_margins.vertical_m
    timestamp = overlap_start
    while timestamp <= overlap_end:
        first_position = _position_at(first, timestamp)
        if first_position is None:
            first_position = _position_at(
                first, min(max(timestamp, first[0][3]), first[-1][3])
            )
        second_position = _position_at(second, timestamp)
        if second_position is None:
            second_position = _position_at(
                second, min(max(timestamp, second[0][3]), second[-1][3])
            )
        if first_position and second_position:
            horizontal = math.hypot(
                first_position[0] - second_position[0],
                first_position[1] - second_position[1],
            )
            vertical = abs(first_position[2] - second_position[2])
            if horizontal < horizontal_limit and vertical < vertical_limit:
                return True
        timestamp += max(sample_interval_s, 0.25)
    return False


def point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    projection = (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    ) / length_sq
    projection = max(0.0, min(1.0, projection))
    closest = (start[0] + projection * dx, start[1] + projection * dy)
    return math.hypot(point[0] - closest[0], point[1] - closest[1])


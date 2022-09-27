from math import sqrt
from typing import Callable


def calc_global_level(xp: int) -> int:
    return int(sqrt(xp // 20))


def calc_global_xp_needed(level: int) -> int:
    return level**2 * 20


def calc_root_skill_level(xp: int) -> int:
    return int(sqrt(xp // 20))


def calc_root_skill_xp_needed(level: int) -> int:
    return level**2 * 20


def calc_sub_skill_level(xp: int) -> int:
    return int(sqrt(xp // 20))


def calc_sub_skill_xp_needed(level: int) -> int:
    return level**2 * 20


def calc_progress(xp: int, level: int, level_to_xp: Callable[[int], int]) -> float:
    lower = level_to_xp(level)
    upper = level_to_xp(level + 1)
    return (xp - lower) / (upper - lower)

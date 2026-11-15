from math import ceil

def deletion_budget(previous_count: int, pct: float = 5, absolute: int = 20) -> int:
    """Maximum removals allowed in one complete provider/scope snapshot."""
    if previous_count <= 0:
        return 0
    return min(absolute, max(1, ceil(previous_count * pct / 100)))

def deletion_is_safe(previous_count: int, missing_count: int, pct: float = 5, absolute: int = 20) -> bool:
    return 0 <= missing_count <= deletion_budget(previous_count, pct, absolute)

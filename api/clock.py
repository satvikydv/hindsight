import time


def wall_ms() -> int:
    """Real time in ms. Used only for incident locks, which guard real concurrency; the simulated clock (World.now_ms)
    stamps everything else."""
    return int(time.time() * 1000)

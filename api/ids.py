from ulid import ULID


def new_id() -> str:
    """A fresh ULID. Callers generate ids client-side so retried writes can MERGE instead of duplicating."""
    return str(ULID())

"""Durable JSON persistence helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


def atomic_json_dump(
    data: Any,
    path: str | os.PathLike[str],
    *,
    indent: int | None = 2,
) -> None:
    """Write JSON through a same-directory temporary file and atomic replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            json.dump(data, temporary, indent=indent)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)

        # Persist the directory entry where the platform supports directory fsync.
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

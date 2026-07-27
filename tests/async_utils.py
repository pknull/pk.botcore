"""Run async unit tests without requiring a pytest event-loop plugin."""

import asyncio
from functools import wraps


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run

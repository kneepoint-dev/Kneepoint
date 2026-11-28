

from functools import cache
from importlib.metadata import version


@cache
def user_agent() -> str:
    return f"kneepoint/{version('kneepoint')}"

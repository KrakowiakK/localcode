"""Proxy module that exposes localcode.py attributes."""

from . import localcode as _agent


def __getattr__(name):
    return getattr(_agent, name)


def __setattr__(name, value):
    setattr(_agent, name, value)


def __dir__():
    return sorted(set(dir(_agent)))


__all__ = [name for name in dir(_agent) if not name.startswith("__")]

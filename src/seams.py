"""Dynamic seam resolution for the app.py / llm_evaluator.py refactor.

The test-suite monkeypatches seam names *directly on the facade modules*
(``app`` and ``src.llm_evaluator``). Code that was moved out of those facades
into single-responsibility modules therefore cannot bind seam names with
``from x import y`` - a static binding would ignore the patched value.

Instead, moved code resolves seams at *call time* through :func:`resolve`,
which looks the name up on the facade module currently loaded in
``sys.modules`` and falls back to a local default when the facade is absent
(e.g. when a submodule is imported without the facade, or under Streamlit's
``__main__`` execution where ``app`` is never registered as ``app``).
"""
from __future__ import annotations

import sys
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_MISSING = object()


def resolve(facade: str, name: str, default: T) -> T:
    """Return ``getattr(sys.modules[facade], name, default)``.

    ``default`` is the locally-bound implementation, used when the facade
    module is not loaded (or does not carry the name - e.g. a partially
    initialized facade during a circular import).
    """
    mod = sys.modules.get(facade)
    if mod is not None:
        value = getattr(mod, name, _MISSING)
        if value is not _MISSING:
            return value
    return default


class SeamedCallable:
    """Call-through proxy for a monkeypatchable *function* seam.

    Resolves the real callable from the facade on every call and forwards
    both the call and any attribute access (``.clear()`` on an
    ``st.cache_data``-wrapped loader, etc.) to the resolved object.
    """

    __slots__ = ("facade", "name", "default")

    def __init__(self, facade: str, name: str, default: Callable[..., Any]) -> None:
        self.facade = facade
        self.name = name
        self.default = default

    def resolved(self) -> Callable[..., Any]:
        value = resolve(self.facade, self.name, self.default)
        # If the facade re-exports *this very proxy* (a thin shim imports the
        # proxy under the public name), calling it would recurse forever.
        # Fall back to the locally-bound implementation in that case.
        if value is self:
            return self.default
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.resolved()(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        # Only reached for names outside __slots__ (e.g. ``clear``).
        return getattr(self.resolved(), item)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SeamedCallable {self.facade}.{self.name}>"


def set_on_facade(facade: str, name: str, value: Any, local_default: Any) -> Any:
    """Write a mutable seam (module-level cache) back to the facade if loaded.

    Mutable seams (``_client``, ``_ROUTER``, ...) are *owned* by the facade:
    readers resolve them from the facade, so a writer must update the facade's
    binding or the write would be invisible. Also updates ``local_default``'s
    owning module global via the returned value for the non-facade path.
    """
    mod = sys.modules.get(facade)
    if mod is not None and hasattr(mod, name):
        setattr(mod, name, value)
    return value


def seam_fn(facade: str, name: str, default: Callable[..., Any]) -> SeamedCallable:
    """Shorthand: a call-through function seam resolved from ``facade``."""
    return SeamedCallable(facade, name, default)


class _StProxy:
    """Attribute-forwarding proxy for ``st`` (monkeypatched as ``app.st``).

    Nearly every UI helper calls ``st.<something>``. Binding ``st`` to this
    proxy lets moved code keep ``st.warning(...)`` verbatim while resolving the
    *facade's* ``st`` (a test stub, or real Streamlit) at attribute-access time.
    Falls back to real Streamlit when the facade is absent, so import-time
    decorators like ``@st.cache_data`` still bind correctly.
    """

    __slots__ = ("_facade", "_fallback")

    def __init__(self, facade: str, fallback: Any) -> None:
        self._facade = facade
        self._fallback = fallback

    def _target(self) -> Any:
        return resolve(self._facade, "st", self._fallback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


def st_proxy(facade: str, fallback: Any) -> _StProxy:
    """Shorthand: an ``st`` attribute proxy resolved from ``facade``."""
    return _StProxy(facade, fallback)


class SeamDict:
    """Dict-mutation proxy for a seam like ``_CATCHUP_STATE``.

    The facade owns the dict (tests replace it wholesale via
    ``monkeypatch.setattr(app, "_CATCHUP_STATE", {...})``). Moved code does
    ``state = _CATCHUP_STATE; state["attempted"] = True`` - so the proxy must
    forward ``__getitem__``/``__setitem__``/``get``/etc. to the *current* facade
    dict rather than a snapshot captured at import time.
    """

    __slots__ = ("_facade", "_name", "_default")

    def __init__(self, facade: str, name: str, default: dict) -> None:
        self._facade = facade
        self._name = name
        self._default = default

    def _d(self) -> dict:
        value = resolve(self._facade, self._name, self._default)
        if value is self:
            return self._default
        return value

    def __getitem__(self, key: Any) -> Any:
        return self._d()[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._d()[key] = value

    def get(self, key: Any, default: Any = None) -> Any:
        return self._d().get(key, default)

    def __contains__(self, key: Any) -> bool:
        return key in self._d()

    def __bool__(self) -> bool:
        return bool(self._d())

    def __iter__(self):
        return iter(self._d())

    def __len__(self) -> int:
        return len(self._d())

    def keys(self):
        return self._d().keys()

    def values(self):
        return self._d().values()

    def items(self):
        return self._d().items()

    def copy(self) -> dict:
        return self._d().copy()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SeamDict {self._facade}.{self._name} -> {self._d()!r}>"


def seam_dict(facade: str, name: str, default: dict) -> SeamDict:
    """Shorthand: a dict-mutation seam resolved from ``facade``."""
    return SeamDict(facade, name, default)


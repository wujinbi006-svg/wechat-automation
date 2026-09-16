"""Provider Registry — runtime registration and lookup.

Future breakthrough implementations register here; the rest of
the system queries the registry, never importing concrete classes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ProviderEntry:
    name: str
    interface: str
    factory: Any  # callable() -> provider instance
    priority: int = 0
    active: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderRegistry:
    """Thread-safe provider registry."""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[str, list[ProviderEntry]] = {}

    def register(self, interface: str, name: str, factory: Any,
                 priority: int = 0, metadata: dict | None = None) -> None:
        with self._lock:
            entries = self._entries.setdefault(interface, [])
            # Remove existing with same name
            entries[:] = [e for e in entries if e.name != name]
            entries.append(ProviderEntry(
                name=name, interface=interface, factory=factory,
                priority=priority, metadata=metadata or {},
            ))
            # Sort by priority (higher = preferred)
            entries.sort(key=lambda e: -e.priority)

    def unregister(self, interface: str, name: str) -> None:
        with self._lock:
            entries = self._entries.get(interface, [])
            entries[:] = [e for e in entries if e.name != name]

    def get(self, interface: str) -> Any | None:
        """Get the highest-priority active provider for an interface."""
        with self._lock:
            entries = self._entries.get(interface, [])
            for entry in entries:
                if entry.active:
                    try:
                        return entry.factory()
                    except Exception:
                        continue
            # Fall back to highest-priority even if not marked active
            for entry in entries:
                try:
                    return entry.factory()
                except Exception:
                    continue
            return None

    def list(self, interface: str | None = None) -> list[ProviderEntry]:
        with self._lock:
            if interface:
                return list(self._entries.get(interface, []))
            return [e for entries in self._entries.values() for e in entries]

    def set_active(self, interface: str, name: str) -> None:
        with self._lock:
            for entry in self._entries.get(interface, []):
                entry.active = (entry.name == name)

    def interfaces(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry

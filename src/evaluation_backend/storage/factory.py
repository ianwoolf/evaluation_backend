"""Composition boundary for storage adapters, including future remote services.

Adapters implement Storage, normalize vendor failures to StorageError subclasses,
and enforce project isolation and optimistic revisions. Never expose SDK types.
Register adapters in the API composition root before resolving get_service().
"""
from typing import Callable
from . import Storage

_factories: dict[str, Callable[[], Storage]] = {}


def register_storage(name: str, factory: Callable[[], Storage]):
    if not name or name in _factories or name == 'elasticsearch':
        raise ValueError('Storage adapter name is empty or already registered')
    _factories[name] = factory


def create_storage(name: str) -> Storage:
    if name == 'elasticsearch':
        from .elastic import ElasticsearchStorage
        storage = ElasticsearchStorage.from_env()
        storage.initialize()
        return storage
    if name not in _factories:
        raise ValueError('Unknown storage adapter: ' + name)
    return _factories[name]()

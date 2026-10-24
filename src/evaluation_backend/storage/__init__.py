"""Storage port. Business code must not depend on a storage vendor."""
from typing import Protocol


class StorageError(Exception):
    pass


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    pass


class Storage(Protocol):
    def get(self, collection: str, project: str, ident: str) -> dict: ...
    def put(self, collection: str, project: str, ident: str, value: dict, *, create: bool = False,
            revision: str | None = None) -> dict: ...
    def search(self, collection: str, project: str, *, filters: dict | None = None,
               offset: int = 0, limit: int = 100) -> dict: ...

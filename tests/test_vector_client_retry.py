from __future__ import annotations

import itertools

import pytest

from app.infra.vector.client import VectorClient


class DummyVectorBackend:
    def __init__(self, fail_upsert=0, fail_delete=0, fail_search=0):
        self._upsert_count = itertools.count()
        self._delete_count = itertools.count()
        self._search_count = itertools.count()
        self.fail_upsert = fail_upsert
        self.fail_delete = fail_delete
        self.fail_search = fail_search
        self.deleted = []

    def upsert(self, items):
        if next(self._upsert_count) < self.fail_upsert:
            raise RuntimeError("upsert boom")
        return len(list(items))

    def delete(self, emb_id):
        if next(self._delete_count) < self.fail_delete:
            raise RuntimeError("delete boom")
        self.deleted.append(emb_id)
        return True

    def search(self, query, k=5):
        if next(self._search_count) < self.fail_search:
            raise RuntimeError("search boom")
        return [{"id": 1, "score": 0.9}]


def test_vector_upsert_retries_and_succeeds():
    backend = DummyVectorBackend(fail_upsert=2)
    client = VectorClient(backend=backend, max_retries=3, backoff_seconds=0)
    assert client.upsert([{"id": "1"}]) == 1


def test_vector_upsert_raises_after_retries():
    backend = DummyVectorBackend(fail_upsert=5)
    client = VectorClient(backend=backend, max_retries=1, backoff_seconds=0)
    with pytest.raises(RuntimeError):
        client.upsert([{"id": "1"}])


def test_vector_search_retries_and_succeeds():
    backend = DummyVectorBackend(fail_search=1)
    client = VectorClient(backend=backend, max_retries=2, backoff_seconds=0)
    res = client.search("q", k=1)
    assert res and res[0]["id"] == 1

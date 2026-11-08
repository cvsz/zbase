"""Regression tests for service-layer coupling findings.

These tests exercise the application services with in-memory fakes so they need
no database or storage engine. They lock in the behaviour of the findings they
regress:

* ZB-ARC-02: ``BucketService`` receives its bucket limit through its
  constructor instead of re-loading configuration mid-request (removes a local
  import that worked around an import cycle).
* ZB-ARC-01 / ZB-009: ``UsageService`` reads engine capabilities through the
  public ``StorageAdapter.capabilities`` property instead of reaching into the
  private ``_capabilities`` attribute.
"""

from __future__ import annotations

import uuid

import pytest

from s3vault.errors import ConflictError
from s3vault.security.rbac import Capability
from s3vault.services.buckets import BucketService
from s3vault.services.users import Actor
from s3vault.services.usage import UsageService
from s3vault.storage.base import Capabilities, EngineUsage, Feature



class _FakeDb:
    """In-memory stand-in for ``Database`` used only by the create path."""

    def __init__(self, bucket_count: int = 0) -> None:
        self._bucket_count = bucket_count
        self.created_buckets: list[str] = []

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        if "SELECT 1 FROM buckets WHERE name = %s" in query:
            return None  # not yet managed by S3Vault
        if "SELECT count(*)" in query:
            return {"n": self._bucket_count}
        if "INSERT INTO buckets" in query:
            self._bucket_count += 1
            self.created_buckets.append(params[0])
            return {
                "id": str(uuid.uuid4()),
                "name": params[0],
                "owner_id": params[1],
                "created_by": params[2],
                "engine_confirmed_at": None,
            }
        raise AssertionError(f"unexpected query: {query!r}")

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        return []

    def execute(self, query: str, params: tuple = ()) -> int:
        return 0


class _FakeAdapter:
    """In-memory stand-in for a ``StorageAdapter``."""

    def __init__(self, capabilities: Capabilities | None = None) -> None:
        self._capabilities = capabilities
        self.created_buckets: list[str] = []

    @property
    def capabilities(self) -> Capabilities | None:
        return self._capabilities

    def probe(self) -> Capabilities:  # pragma: no cover - not used here
        raise NotImplementedError

    def healthy(self) -> bool:  # pragma: no cover - not used here
        return True

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.created_buckets

    def create_bucket(self, bucket: str) -> None:
        self.created_buckets.append(bucket)

    def usage(self):
        return EngineUsage(measured=False, source="unavailable")


def _caps(supported: frozenset) -> Capabilities:
    return Capabilities(
        engine_name="fake",
        engine_version="1.0",
        supported=supported,
        notes={},
    )


# ---------------------------------------------------------------------------
# ZB-ARC-02: bucket limit is injected, not re-loaded from configuration
# ---------------------------------------------------------------------------
class TestBucketLimitIsInjected:
    def test_create_up_to_the_limit_succeeds(self):
        db = _FakeDb(bucket_count=0)
        adapter = _FakeAdapter()
        svc = BucketService(
            db,
            adapter,
            _caps(frozenset({Capability.BUCKET_WRITE})),
            max_buckets_per_user=2,
        )
        actor = Actor(user_id="u1", username="u1", role="owner")

        row1 = svc.create(name="bucket-one", owner_id="u1", actor=actor)
        row2 = svc.create(name="bucket-two", owner_id="u1", actor=actor)
        assert row1["name"] == "bucket-one"
        assert row2["name"] == "bucket-two"
        assert adapter.created_buckets == ["bucket-one", "bucket-two"]

    def test_create_beyond_the_limit_is_rejected(self):
        db = _FakeDb(bucket_count=2)
        adapter = _FakeAdapter()
        svc = BucketService(
            db,
            adapter,
            _caps(frozenset({Capability.BUCKET_WRITE})),
            max_buckets_per_user=2,
        )
        actor = Actor(user_id="u1", username="u1", role="owner")

        with pytest.raises(ConflictError) as excinfo:
            svc.create(name="bucket-three", owner_id="u1", actor=actor)
        assert "bucket limit reached" in str(excinfo.value).lower()
        assert "bucket-three" not in adapter.created_buckets


# ---------------------------------------------------------------------------
# ZB-ARC-01 / ZB-009: capabilities are read through the public property
# ---------------------------------------------------------------------------
class TestCapabilitiesReadThroughPublicProperty:
    def test_engine_capacity_unavailable_when_not_probed(self):
        """No capabilities cached yet: report unavailable, do not probe."""
        adapter = _FakeAdapter(capabilities=None)
        svc = UsageService(None, adapter)  # type: ignore[arg-type]
        report = svc.engine_capacity()
        assert report["available"] is False
        assert report["reason"] == "unavailable"

    def test_engine_capacity_unavailable_when_engine_lacks_metrics(self):
        """The engine was probed and reports no usage-metrics capability."""
        adapter = _FakeAdapter(
            capabilities=_caps(frozenset({Capability.BUCKET_READ}))
        )
        svc = UsageService(None, adapter)  # type: ignore[arg-type]
        report = svc.engine_capacity()
        assert report["available"] is False
        assert report["reason"] == "engine does not expose usage metrics"

    def test_engine_capacity_available_when_engine_reports_it(self):
        adapter = _FakeAdapter(
            capabilities=_caps(frozenset({Feature.ENGINE_USAGE_METRICS}))
        )

        class _WithUsage(_FakeAdapter):
            def usage(self):
                return EngineUsage(
                    measured=True,
                    bytes_used=1234,
                    capacity_bytes=5678,
                    source="fake-engine",
                )

        svc = UsageService(None, _WithUsage(adapter.capabilities))  # type: ignore[arg-type]
        report = svc.engine_capacity()
        assert report["available"] is True
        assert report["capacity_bytes"] == 5678
        assert report["used_bytes"] == 1234
        assert report["source"] == "fake-engine"

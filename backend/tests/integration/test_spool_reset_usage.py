"""Reset-usage endpoint regressions (#1390 follow-up).

The per-spool and bulk reset endpoints zero `weight_used` without touching
`weight_locked`. They exist because PATCH /spools/{id} auto-locks the spool
when weight_used is set explicitly, and that's wrong for the "clean-slate
my Total Consumed stat" workflow — the user wants the spool to keep
receiving AMS auto-sync updates from the next print onward.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool


@pytest.fixture
async def spool_factory(db_session: AsyncSession):
    """Create a Spool with sensible defaults."""

    async def _create(**kwargs):
        defaults = {
            "material": "PLA",
            "subtype": "Basic",
            "brand": "Bambu",
            "color_name": "Red",
            "rgba": "FF0000FF",
            "label_weight": 1000,
            "weight_used": 0,
            "weight_locked": False,
        }
        defaults.update(kwargs)
        spool = Spool(**defaults)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create


class TestResetSpoolUsage:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reset_zeroes_weight_used(self, async_client: AsyncClient, spool_factory, db_session):
        """Endpoint sets weight_used to 0."""
        spool = await spool_factory(weight_used=234.5)

        response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/reset-usage")

        assert response.status_code == 200
        assert response.json()["weight_used"] == 0
        await db_session.refresh(spool)
        assert spool.weight_used == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reset_does_not_lock_spool(self, async_client: AsyncClient, spool_factory, db_session):
        """Reset must leave weight_locked alone.

        PATCH /spools/{id} auto-locks when weight_used is set explicitly;
        the dedicated reset endpoint must NOT, because the user's intent
        is "track fresh from zero", not "freeze at zero forever".
        """
        spool = await spool_factory(weight_used=100.0, weight_locked=False)

        response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/reset-usage")

        assert response.status_code == 200
        await db_session.refresh(spool)
        assert spool.weight_used == 0
        assert spool.weight_locked is False, "Reset must not auto-lock the spool"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reset_preserves_existing_lock(self, async_client: AsyncClient, spool_factory, db_session):
        """If the user previously locked the spool, the lock is preserved."""
        spool = await spool_factory(weight_used=500.0, weight_locked=True)

        response = await async_client.post(f"/api/v1/inventory/spools/{spool.id}/reset-usage")

        assert response.status_code == 200
        await db_session.refresh(spool)
        assert spool.weight_used == 0
        assert spool.weight_locked is True, "Pre-existing lock must be preserved"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reset_404_for_missing_spool(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/inventory/spools/99999/reset-usage")
        assert response.status_code == 404


class TestBulkResetSpoolUsage:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_reset_zeroes_only_listed_spools(self, async_client: AsyncClient, spool_factory, db_session):
        """Only spools in the request are reset; others are untouched."""
        target1 = await spool_factory(weight_used=100.0)
        target2 = await spool_factory(weight_used=200.0)
        untouched = await spool_factory(weight_used=300.0)

        response = await async_client.post(
            "/api/v1/inventory/spools/reset-usage-bulk",
            json={"spool_ids": [target1.id, target2.id]},
        )

        assert response.status_code == 200
        assert response.json() == {"reset": 2}

        # The endpoint commits via its own session — expire our session so the
        # next read pulls fresh values rather than the cached pre-reset state.
        db_session.expire_all()
        spools = (await db_session.execute(select(Spool))).scalars().all()
        by_id = {s.id: s for s in spools}
        assert by_id[target1.id].weight_used == 0
        assert by_id[target2.id].weight_used == 0
        assert by_id[untouched.id].weight_used == 300.0, "Spool not in request must keep its usage"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_reset_rejects_empty_list(self, async_client: AsyncClient):
        """Empty list must be rejected — guards against accidental wildcard wipes."""
        response = await async_client.post(
            "/api/v1/inventory/spools/reset-usage-bulk",
            json={"spool_ids": []},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_reset_rejects_missing_field(self, async_client: AsyncClient):
        """Missing spool_ids field must be rejected."""
        response = await async_client.post(
            "/api/v1/inventory/spools/reset-usage-bulk",
            json={},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_reset_does_not_lock_spools(self, async_client: AsyncClient, spool_factory, db_session):
        """Bulk reset preserves weight_locked across all targets."""
        unlocked = await spool_factory(weight_used=100.0, weight_locked=False)
        locked = await spool_factory(weight_used=200.0, weight_locked=True)

        response = await async_client.post(
            "/api/v1/inventory/spools/reset-usage-bulk",
            json={"spool_ids": [unlocked.id, locked.id]},
        )

        assert response.status_code == 200
        await db_session.refresh(unlocked)
        await db_session.refresh(locked)
        assert unlocked.weight_used == 0 and unlocked.weight_locked is False
        assert locked.weight_used == 0 and locked.weight_locked is True

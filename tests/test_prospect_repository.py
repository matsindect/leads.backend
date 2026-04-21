"""Integration tests for ProspectRepository — target_companies & target_people.

Uses testcontainers for real Postgres semantics (ON CONFLICT, JSONB, ARRAY).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from domain.models import TargetCompany, TargetPerson
from infrastructure.postgres_repo import PostgresLeadRepository, metadata


@pytest.fixture(scope="module")
def postgres_url() -> str:
    with PostgresContainer("postgres:16-alpine") as pg:
        sync_url = pg.get_connection_url()
        async_url = sync_url.replace("psycopg2", "asyncpg")
        yield async_url  # type: ignore[misc]


@pytest_asyncio.fixture
async def db_session_factory(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def repo(db_session_factory: async_sessionmaker[AsyncSession]) -> PostgresLeadRepository:
    return PostgresLeadRepository(db_session_factory)


def _company(source_id: str, **kwargs: object) -> TargetCompany:
    return TargetCompany(
        source="linkedin",
        source_id=source_id,
        name=kwargs.pop("name", f"Acme {source_id}"),  # type: ignore[arg-type]
        raw_payload={"company_id": source_id},
        **kwargs,  # type: ignore[arg-type]
    )


def _person(source_id: str, **kwargs: object) -> TargetPerson:
    return TargetPerson(
        source="linkedin",
        source_id=source_id,
        full_name=kwargs.pop("full_name", f"Person {source_id}"),  # type: ignore[arg-type]
        raw_payload={"profile_id": source_id},
        **kwargs,  # type: ignore[arg-type]
    )


class TestTargetCompanies:

    @pytest.mark.asyncio
    async def test_upsert_inserts_new(self, repo: PostgresLeadRepository) -> None:
        inserted, duplicates = await repo.upsert_target_companies(
            [_company("c1", domain="acme.io"), _company("c2", domain="beta.io")]
        )
        assert len(inserted) == 2
        assert duplicates == 0

    @pytest.mark.asyncio
    async def test_upsert_skips_duplicates(
        self, repo: PostgresLeadRepository
    ) -> None:
        await repo.upsert_target_companies([_company("dup", domain="d.io")])
        inserted, duplicates = await repo.upsert_target_companies(
            [_company("dup", domain="d.io"), _company("new", domain="n.io")]
        )
        assert len(inserted) == 1
        assert duplicates == 1

    @pytest.mark.asyncio
    async def test_list_paginates_and_counts(
        self, repo: PostgresLeadRepository
    ) -> None:
        await repo.upsert_target_companies(
            [_company(f"p{i}") for i in range(5)]
        )
        rows, total = await repo.list_target_companies(limit=2, offset=0)
        assert len(rows) == 2
        assert total >= 5


class TestTargetPeople:

    @pytest.mark.asyncio
    async def test_upsert_people(self, repo: PostgresLeadRepository) -> None:
        inserted, duplicates = await repo.upsert_target_people(
            [
                _person("u1", current_company_domain="acme.io"),
                _person("u2", current_company_domain="beta.io"),
            ]
        )
        assert len(inserted) == 2
        assert duplicates == 0

    @pytest.mark.asyncio
    async def test_list_filter_by_domain(
        self, repo: PostgresLeadRepository
    ) -> None:
        await repo.upsert_target_people(
            [
                _person("f1", current_company_domain="matchme.io"),
                _person("f2", current_company_domain="matchme.io"),
                _person("f3", current_company_domain="other.io"),
            ]
        )
        rows, total = await repo.list_target_people(company_domain="matchme.io")
        assert total == 2
        for r in rows:
            assert r["current_company_domain"] == "matchme.io"

    @pytest.mark.asyncio
    async def test_list_pagination(self, repo: PostgresLeadRepository) -> None:
        await repo.upsert_target_people([_person(f"pp{i}") for i in range(3)])
        rows, total = await repo.list_target_people(limit=1, offset=0)
        assert len(rows) == 1
        assert total >= 3

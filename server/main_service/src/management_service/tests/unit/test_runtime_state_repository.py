"""RuntimeStateRepository의 자원 snapshot 저장 동작을 검증한다.

실제 DB 대신 commit 순서를 제어할 수 있는 비동기 세션 fake를 사용해 같은 자원의
저장 순서 보장과 장비 task_type의 DB 문자열 변환을 확인한다.
"""

from __future__ import annotations

import asyncio

from services.contracts.enums import TaskType
from services.persistence import runtime_state_repository as repository_module
from services.persistence.runtime_state_repository import RuntimeStateRepository


class _TransStat:
    """이송 자원 상태 행에 필요한 필드만 제공하는 테스트 모델."""

    def __init__(self, *, res_id: str) -> None:
        self.res_id = res_id
        self.item_id: int | None = None
        self.cur_stat = "IDLE"
        self.updated_at = None


class _SnapshotSession:
    """commit 대기 시점을 제어해 자원 snapshot의 동시 저장을 재현하는 세션 fake."""

    def __init__(self, factory: "_SnapshotSessionFactory", index: int) -> None:
        self._factory = factory
        self._index = index
        self._stat: _TransStat | None = None

    async def __aenter__(self) -> "_SnapshotSession":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def get(self, model, res_id: str) -> _TransStat:
        current = self._factory.committed[res_id]
        self._stat = model(res_id=res_id)
        self._stat.item_id = current.item_id
        self._stat.cur_stat = current.cur_stat
        self._stat.updated_at = current.updated_at
        return self._stat

    def add(self, stat: _TransStat) -> None:
        self._stat = stat

    async def commit(self) -> None:
        """첫 세션의 commit만 지연하고 완료된 snapshot을 저장소에 반영한다."""

        assert self._stat is not None
        if self._index == 0:
            self._factory.first_commit_started.set()
            await self._factory.release_first_commit.wait()
        self._factory.committed[self._stat.res_id] = self._stat


class _SnapshotSessionFactory:
    """세션 생성 순서와 자원별 최종 commit 상태를 기록하는 factory 역할의 fake."""

    def __init__(self) -> None:
        self.committed = {
            "TAT1": _TransStat(res_id="TAT1"),
            "TAT2": _TransStat(res_id="TAT2"),
        }
        self.first_commit_started = asyncio.Event()
        self.release_first_commit = asyncio.Event()
        self.session_count = 0

    def __call__(self) -> _SnapshotSession:
        session = _SnapshotSession(self, self.session_count)
        self.session_count += 1
        return session


def _repository(factory: _SnapshotSessionFactory) -> RuntimeStateRepository:
    """이송 자원 snapshot 검증에 필요한 의존성만 주입한 repository를 생성한다."""

    return RuntimeStateRepository(
        sync_session_factory=None,
        async_session_factory=factory,
        ord_model=None,
        ord_detail_model=None,
        ord_stat_model=None,
        ord_log_model=None,
        pattern_model=None,
        item_model=None,
        equip_task_txn_model=None,
        trans_stat_model=_TransStat,
    )


def test_resource_snapshots_are_serialized_per_resource() -> None:
    """같은 자원의 최신 snapshot은 보존하고 다른 자원의 저장은 막지 않는지 검증한다."""

    async def scenario() -> None:
        factory = _SnapshotSessionFactory()
        repo = _repository(factory)

        old_snapshot = asyncio.create_task(
            repo.sync_resource_snapshot(
                {"res_id": "TAT1", "item_id": None, "status": "IDLE"}
            )
        )
        await factory.first_commit_started.wait()

        latest_snapshot = asyncio.create_task(
            repo.sync_resource_snapshot(
                {"res_id": "TAT1", "item_id": 22, "status": "ALLOC"}
            )
        )
        other_resource = asyncio.create_task(
            repo.sync_resource_snapshot(
                {"res_id": "TAT2", "item_id": 33, "status": "ALLOC"}
            )
        )
        await asyncio.sleep(0)

        assert factory.session_count == 2
        assert factory.committed["TAT2"].item_id == 33

        factory.release_first_commit.set()
        await asyncio.gather(old_snapshot, latest_snapshot, other_resource)

        assert factory.committed["TAT1"].item_id == 22
        assert factory.committed["TAT1"].cur_stat == "ALLOC"

    asyncio.run(scenario())


class _Column:
    """SQLAlchemy 컬럼 비교식을 대신하는 최소 표현식 fake."""

    def __eq__(self, other):
        return ("eq", other)


class _EquipStat:
    """장비 자원 상태 행에 필요한 필드만 제공하는 테스트 모델."""

    res_id = _Column()

    def __init__(self, *, res_id: str) -> None:
        self.res_id = res_id
        self.item_id: int | None = None
        self.txn_type: str | None = None
        self.cur_stat = "IDLE"
        self.updated_at = None


class _Select:
    """repository의 filter 호출을 수용하는 조회문 fake."""

    def filter(self, _condition):
        return self


class _ScalarResult:
    """조회된 장비 상태 한 건을 반환하는 scalar 결과 fake."""

    def __init__(self, stat: _EquipStat | None) -> None:
        self._stat = stat

    def first(self) -> _EquipStat | None:
        return self._stat


class _ExecuteResult:
    """scalars 호출을 통해 장비 상태 조회 결과를 노출하는 execute 결과 fake."""

    def __init__(self, stat: _EquipStat | None) -> None:
        self._stat = stat

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._stat)


class _EquipSnapshotSession:
    """장비 snapshot의 조회와 신규 상태 추가를 기록하는 비동기 세션 fake."""

    def __init__(self, factory: "_EquipSnapshotSessionFactory") -> None:
        self._factory = factory

    async def __aenter__(self) -> "_EquipSnapshotSession":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, _query) -> _ExecuteResult:
        return _ExecuteResult(self._factory.stat)

    def add(self, stat: _EquipStat) -> None:
        self._factory.stat = stat

    async def commit(self) -> None:
        return None


class _EquipSnapshotSessionFactory:
    """장비 snapshot 세션이 공유할 최종 상태를 보관하는 factory 역할의 fake."""

    def __init__(self) -> None:
        self.stat: _EquipStat | None = None

    def __call__(self) -> _EquipSnapshotSession:
        return _EquipSnapshotSession(self)


def test_equip_resource_snapshot_converts_task_type_to_db_string(monkeypatch) -> None:
    """TaskType enum이 장비 상태의 txn_type 문자열로 저장되는지 검증한다."""

    async def scenario() -> None:
        factory = _EquipSnapshotSessionFactory()
        repo = RuntimeStateRepository(
            sync_session_factory=None,
            async_session_factory=factory,
            ord_model=None,
            ord_detail_model=None,
            ord_stat_model=None,
            ord_log_model=None,
            pattern_model=None,
            item_model=None,
            equip_task_txn_model=None,
            equip_stat_model=_EquipStat,
        )
        monkeypatch.setattr(repository_module, "select", lambda _model: _Select())

        await repo.sync_resource_snapshot(
            {
                "res_id": "PAT1",
                "item_id": 1001,
                "status": "ALLOC",
                "task_type": TaskType.PP,
            }
        )

        assert factory.stat is not None
        assert factory.stat.txn_type == TaskType.PP.value
        assert type(factory.stat.txn_type) is str

    asyncio.run(scenario())

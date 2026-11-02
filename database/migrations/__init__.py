"""轻量数据库迁移(阶段 7)。

不用 Alembic:版本号字符串 + `schema_migrations` 表记录已应用版本;
每个迁移是 `(version, fn(engine))` 元组,顺序执行、幂等。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from database.models import Base, SchemaMigration

Migration = tuple[str, Callable[[Engine], None]]


def _migration_0001_create_all(engine: Engine) -> None:
    """初始 schema:11 张审计/运行表。"""
    Base.metadata.create_all(engine)


MIGRATIONS: list[Migration] = [
    ("0001_create_all", _migration_0001_create_all),
]


def applied_versions(engine: Engine) -> set[str]:
    """已应用版本集合(schema_migrations 不存在时返回空集)。"""
    Base.metadata.create_all(engine, tables=[SchemaMigration.__table__])
    with Session(engine) as session:
        rows = session.scalars(select(SchemaMigration)).all()
        return {row.version for row in rows}


def apply_migrations(engine: Engine) -> list[str]:
    """按顺序应用未执行的迁移,返回本次应用的版本号列表(幂等)。"""
    done = applied_versions(engine)
    fresh: list[str] = []
    now_ms = int(time.time() * 1000)
    for version, fn in MIGRATIONS:
        if version in done:
            continue
        fn(engine)
        with Session(engine) as session:
            session.add(SchemaMigration(version=version, applied_at=now_ms))
            session.commit()
        fresh.append(version)
    return fresh

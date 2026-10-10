from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base, EventLog

_engines: dict[str, Engine] = {}


def engine() -> Engine:
    url = get_settings().database_url
    if url not in _engines:
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            if ":///" in url and not url.endswith(":memory:"):
                from pathlib import Path

                Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_size=20, max_overflow=20, pool_pre_ping=True)
        eng = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(eng, "connect")
            def _pragma(conn, _):  # pragma: no cover - trivial
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _engines[url] = eng
    return _engines[url]


def reset_engines() -> None:
    for e in _engines.values():
        e.dispose()
    _engines.clear()


def init_db() -> None:
    Base.metadata.create_all(engine())


def is_postgres() -> bool:
    return engine().dialect.name == "postgresql"


@contextmanager
def session() -> Iterator[Session]:
    s = sessionmaker(bind=engine(), expire_on_commit=False)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def log_event(s: Session, actor: str, action: str, submission_id: int | None = None, **detail) -> None:
    s.add(EventLog(actor=actor, action=action, submission_id=submission_id, detail=detail))


def get_or_none(s: Session, model, **where):
    return s.scalars(select(model).filter_by(**where)).first()

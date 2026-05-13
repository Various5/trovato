from sqlmodel import select

from app.database import init_db, session_scope
from app.models import DocumentSource, SourceType


def test_create_source_round_trip() -> None:
    init_db()
    with session_scope() as session:
        s = DocumentSource(name="t-src", type=SourceType.local, path="/tmp")
        session.add(s)
        session.flush()
        sid = s.id
    with session_scope() as session:
        loaded = session.exec(select(DocumentSource).where(DocumentSource.id == sid)).first()
        assert loaded is not None
        assert loaded.name == "t-src"
        assert loaded.type == SourceType.local

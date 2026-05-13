from app.database import init_db, session_scope
from app.models import Chat, ChatMessage, User, UserRole
from app.services.exports import (
    chat_to_markdown,
    search_hits_to_csv,
    search_hits_to_json,
)
from app.services.search_service import SearchHit


def test_chat_to_markdown_handles_missing_metadata() -> None:
    init_db()
    with session_scope() as session:
        u = User(username="tester_export", password_hash="x", role=UserRole.user)
        session.add(u)
        session.flush()
        c = Chat(user_id=u.id, title=None)  # type: ignore[arg-type]
        session.add(c)
        session.flush()
        cid = c.id
        session.add(ChatMessage(chat_id=cid, role="user", content="hi"))
        session.add(ChatMessage(chat_id=cid, role="assistant", content="hello"))

    md = chat_to_markdown(cid)
    assert "# Untitled chat" in md
    assert "**You**" in md
    assert "**Assistant**" in md


def _hit(i: int = 0) -> SearchHit:
    return SearchHit(
        chunk_id=i,
        document_id=i,
        filename=f"f{i}.pdf",
        path=f"/tmp/f{i}.pdf",
        page_from=1,
        page_to=1,
        snippet="some text",
        score=0.5,
        source="native_text",
        tags=["lang:en"],
    )


def test_search_csv_has_header() -> None:
    csv = search_hits_to_csv([_hit(1), _hit(2)])
    first = csv.splitlines()[0]
    assert "chunk_id" in first
    assert "snippet" in first
    assert "lang:en" in csv


def test_search_json_is_valid() -> None:
    import json

    data = json.loads(search_hits_to_json([_hit(1)]))
    assert isinstance(data, list) and data[0]["filename"] == "f1.pdf"

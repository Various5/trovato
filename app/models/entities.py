"""Database entity definitions (SQLModel)."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class SourceType(str, enum.Enum):
    local = "local"
    usb = "usb"
    smb = "smb"
    cloud_sync = "cloud_sync"
    webdav = "webdav"
    sftp = "sftp"
    other = "other"


class DocumentStatus(str, enum.Enum):
    new = "new"
    pending = "pending"
    processing = "processing"
    indexed = "indexed"
    changed = "changed"
    error = "error"
    deleted = "deleted"


class ChunkSource(str, enum.Enum):
    native_text = "native_text"
    ocr_text = "ocr_text"
    image_description = "image_description"
    table = "table"
    metadata = "metadata"


class ScanJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    paused = "paused"
    completed = "completed"
    aborted = "aborted"
    error = "error"


# ---------------------------------------------------------------------------
# Users / settings / memory
# ---------------------------------------------------------------------------


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=80)
    password_hash: str
    role: UserRole = Field(default=UserRole.admin)
    is_active: bool = True
    recovery_key_hash: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: Optional[datetime] = None


class UserSetting(SQLModel, table=True):
    __tablename__ = "user_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    theme: str = "dark"
    language: str = "en"
    answer_length: str = "medium"  # short | medium | long
    preferred_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    preferred_sources: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    memory_enabled: bool = True
    updated_at: datetime = Field(default_factory=utcnow)


class UserMemory(SQLModel, table=True):
    __tablename__ = "user_memory"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    key: str
    value: str = Field(sa_column=Column(Text))
    sensitive: bool = False
    confirmed: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Sources / documents / pages / images / chunks
# ---------------------------------------------------------------------------


class Visibility(str, enum.Enum):
    private = "private"
    shared = "shared"


class DocumentSource(SQLModel, table=True):
    __tablename__ = "document_sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    visibility: Visibility = Field(default=Visibility.shared)
    name: str = Field(index=True, max_length=200)
    type: SourceType = SourceType.local
    path: str
    active: bool = True
    scan_interval_minutes: Optional[int] = None
    last_scan_at: Optional[datetime] = None
    include_patterns: list[str] = Field(default_factory=lambda: ["*.pdf"], sa_column=Column(JSON))
    exclude_patterns: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    file_types: list[str] = Field(default_factory=lambda: ["pdf"], sa_column=Column(JSON))
    recursive: bool = True
    ignore_hidden: bool = True
    max_file_size_mb: Optional[int] = None
    credentials_ref: Optional[str] = None  # opaque reference, never stored in cleartext
    created_at: datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_hash", "content_hash"),
        Index("ix_documents_path", "path"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    visibility: Visibility = Field(default=Visibility.shared)
    source_id: int = Field(foreign_key="document_sources.id", index=True)
    path: str
    filename: str
    extension: str = "pdf"
    size_bytes: int = 0
    content_hash: str = Field(index=True, max_length=64)
    page_count: int = 0
    created_at_fs: Optional[datetime] = None
    modified_at_fs: Optional[datetime] = None
    indexed_at: Optional[datetime] = None
    status: DocumentStatus = DocumentStatus.new
    error: Optional[str] = None
    language: Optional[str] = None
    doc_type: Optional[str] = None  # rechnung, vertrag, ...
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class DocumentPage(SQLModel, table=True):
    __tablename__ = "document_pages"
    __table_args__ = (Index("ix_document_pages_doc_page", "document_id", "page_number", unique=True),)

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True)
    page_number: int
    native_text: str = Field(default="", sa_column=Column(Text))
    ocr_text: str = Field(default="", sa_column=Column(Text))
    has_images: bool = False
    has_tables: bool = False
    rendered_image_path: Optional[str] = None
    width: int = 0
    height: int = 0


class DocumentImage(SQLModel, table=True):
    __tablename__ = "document_images"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True)
    page_number: int
    image_index: int
    image_hash: str = Field(index=True, max_length=64)
    width: int = 0
    height: int = 0
    cache_path: str
    ocr_text: str = Field(default="", sa_column=Column(Text))
    vision_description: str = Field(default="", sa_column=Column(Text))
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class DocumentChunk(SQLModel, table=True):
    __tablename__ = "document_chunks"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True)
    page_from: int = 1
    page_to: int = 1
    text: str = Field(sa_column=Column(Text))
    source: ChunkSource = ChunkSource.native_text
    token_count: int = 0
    embedding_id: Optional[str] = Field(default=None, index=True)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class Tag(SQLModel, table=True):
    __tablename__ = "tags"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=120)
    color: Optional[str] = None
    auto: bool = True
    description: Optional[str] = None


class DocumentTagLink(SQLModel, table=True):
    __tablename__ = "document_tags"

    document_id: int = Field(foreign_key="documents.id", primary_key=True)
    tag_id: int = Field(foreign_key="tags.id", primary_key=True)
    score: float = 1.0
    auto: bool = True


# ---------------------------------------------------------------------------
# Scan jobs
# ---------------------------------------------------------------------------


class ScanJob(SQLModel, table=True):
    __tablename__ = "scan_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: Optional[int] = Field(default=None, foreign_key="document_sources.id", index=True)
    status: ScanJobStatus = ScanJobStatus.queued
    total_files: int = 0
    processed_files: int = 0
    error_count: int = 0
    current_file: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    options: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    message: Optional[str] = None


class ScanJobItem(SQLModel, table=True):
    __tablename__ = "scan_job_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="scan_jobs.id", index=True)
    document_id: Optional[int] = Field(default=None, foreign_key="documents.id")
    path: str
    status: DocumentStatus = DocumentStatus.pending
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


class Chat(SQLModel, table=True):
    __tablename__ = "chats"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    title: str = "New chat"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    pinned: bool = False
    archived: bool = False


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="chats.id", index=True)
    role: str  # user | assistant | system
    content: str = Field(sa_column=Column(Text))
    sources: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    token_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class ChatContextItem(SQLModel, table=True):
    __tablename__ = "chat_context_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="chats.id", index=True)
    kind: str  # document | source | tag | path
    ref_id: Optional[int] = None
    value: Optional[str] = None


# ---------------------------------------------------------------------------
# Backups / app settings / model configs / audit
# ---------------------------------------------------------------------------


class Backup(SQLModel, table=True):
    __tablename__ = "backups"

    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    path: str
    size_bytes: int = 0
    components: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    app_version: str
    db_version: Optional[str] = None
    encrypted: bool = False
    document_count: int = 0
    chunk_count: int = 0
    chat_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str = Field(sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utcnow)


class ModelConfig(SQLModel, table=True):
    __tablename__ = "model_configs"

    id: Optional[int] = Field(default=None, primary_key=True)
    role: str  # chat | vision | embedding
    name: str  # model id reported by LM Studio
    base_url: str = "http://localhost:1234/v1"
    is_default: bool = False
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    event: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    delete,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.contracts import AnalyticalState, QueryResponse
from app.errors import AppError


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(String(100))
    state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_request: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RequestRecord(Base):
    __tablename__ = "query_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    owner: Mapped[str] = mapped_column(String(512), index=True)
    grant_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditRecord(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    owner: Mapped[str] = mapped_column(String(512), index=True)
    event: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Store:
    def __init__(self, url):
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 15} if url.startswith("sqlite") else {},
        )
        self.db = sessionmaker(self.engine, expire_on_commit=False)

    def create_session(self, owner, title):
        with self.db.begin() as db:
            row = Conversation(id=str(uuid4()), owner=owner, title=title)
            db.add(row)
            return row.id

    def conversation(self, owner, session_id):
        with self.db() as db:
            row = db.scalar(
                select(Conversation).where(Conversation.id == session_id, Conversation.owner == owner)
            )
            if row is None:
                raise AppError("not_found", "Conversation not found.", 404)
            return row

    def list_sessions(self, owner):
        with self.db() as db:
            return [
                {"id": r.id, "title": r.title, "updated_at": r.updated_at.isoformat()}
                for r in db.scalars(
                    select(Conversation)
                    .where(Conversation.owner == owner)
                    .order_by(Conversation.updated_at.desc())
                    .limit(100)
                )
            ]

    def reserve(self, response: QueryResponse, owner, grant_hash):
        with self.db.begin() as db:
            changed = db.execute(
                update(Conversation)
                .where(
                    Conversation.id == response.session_id,
                    Conversation.owner == owner,
                    Conversation.active_request.is_(None),
                )
                .values(active_request=response.id, updated_at=now())
            ).rowcount
            if not changed:
                raise AppError(
                    "conversation_busy",
                    "Wait for or cancel the current request before asking a follow-up.",
                    409,
                )
            db.add(
                RequestRecord(
                    id=response.id,
                    session_id=response.session_id,
                    owner=owner,
                    grant_hash=grant_hash,
                    status=response.status,
                    data=response.model_dump(mode="json"),
                )
            )

    def get_request(self, owner, request_id):
        with self.db() as db:
            row = db.scalar(
                select(RequestRecord).where(RequestRecord.id == request_id, RequestRecord.owner == owner)
            )
            if row is None:
                raise AppError("not_found", "Request not found.", 404)
            return row

    def save_request(self, owner, response: QueryResponse):
        with self.db.begin() as db:
            db.execute(
                update(RequestRecord)
                .where(RequestRecord.id == response.id, RequestRecord.owner == owner)
                .values(status=response.status, data=response.model_dump(mode="json"))
            )

    def finish(self, owner, response: QueryResponse, state: AnalyticalState | None):
        with self.db.begin() as db:
            db.execute(
                update(RequestRecord)
                .where(RequestRecord.id == response.id, RequestRecord.owner == owner)
                .values(status=response.status, data=response.model_dump(mode="json"))
            )
            values = {"active_request": None, "updated_at": now()}
            if state:
                values["state"] = state.model_dump(mode="json")
            db.execute(
                update(Conversation)
                .where(
                    Conversation.id == response.session_id,
                    Conversation.owner == owner,
                    Conversation.active_request == response.id,
                )
                .values(**values)
            )

    def history(self, owner, session_id, limit):
        self.conversation(owner, session_id)
        with self.db() as db:
            return list(
                db.scalars(
                    select(RequestRecord)
                    .where(RequestRecord.session_id == session_id, RequestRecord.owner == owner)
                    .order_by(RequestRecord.created_at.desc())
                    .limit(limit)
                )
            )[::-1]

    def audit(self, owner, response: QueryResponse, event, details):
        with self.db.begin() as db:
            db.add(
                AuditRecord(
                    request_id=response.id,
                    correlation_id=response.correlation_id,
                    owner=owner,
                    event=event,
                    details=details,
                )
            )

    def audit_events(self, owner, request_id):
        self.get_request(owner, request_id)
        with self.db() as db:
            return [
                {
                    "event": r.event,
                    "details": r.details,
                    "correlation_id": r.correlation_id,
                    "created_at": r.created_at.isoformat(),
                }
                for r in db.scalars(
                    select(AuditRecord)
                    .where(AuditRecord.owner == owner, AuditRecord.request_id == request_id)
                    .order_by(AuditRecord.id)
                )
            ]

    def recover_interrupted(self):
        # Single worker deployment only. Never report an interrupted execution as successful.
        with self.db.begin() as db:
            for row in db.scalars(
                select(RequestRecord).where(RequestRecord.status.in_(["queued", "running"]))
            ):
                data = dict(row.data)
                data.update(
                    status="failed",
                    stage="interrupted",
                    error={
                        "code": "interrupted",
                        "message": "The worker restarted. Review the query audit before retrying.",
                    },
                )
                row.data, row.status = data, "failed"
            db.execute(update(Conversation).values(active_request=None))

    def purge(self, days):
        cutoff = now() - timedelta(days=days)
        with self.db.begin() as db:
            db.execute(delete(AuditRecord).where(AuditRecord.created_at < cutoff))
            db.execute(
                delete(RequestRecord).where(
                    RequestRecord.created_at < cutoff, RequestRecord.status.not_in(["queued", "running"])
                )
            )
            db.execute(
                delete(Conversation).where(
                    Conversation.updated_at < cutoff, Conversation.active_request.is_(None)
                )
            )

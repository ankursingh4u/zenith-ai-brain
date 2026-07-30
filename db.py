"""Database models and session.

MULTI-TENANT RULE (the heart of "no user can see another's data"):
Every row that belongs to a person carries `telegram_id`, and every query is
filtered by it. Helper functions below always take telegram_id so isolation is
impossible to forget.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, DateTime, Float, ForeignKey, String, Text, create_engine, func, select
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, relationship
)

import config


class Base(DeclarativeBase):
    pass


class User(Base):
    """One approved person. Identified by their Telegram ID."""
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Passed the access passphrase ("Godfather" gate).
    verified: Mapped[bool] = mapped_column(default=False)
    # Optional per-user OWN service-account key (encrypted) — hidden power feature.
    custom_sa_enc: Mapped[Optional[str]] = mapped_column(Text)
    # Optional per-user OWN OAuth client (console) JSON (encrypted) — full control.
    custom_oauth_enc: Mapped[Optional[str]] = mapped_column(Text)
    # Which linked Google account is this user's active/default one.
    default_account: Mapped[Optional[str]] = mapped_column(String(255))
    # Brute-force protection on the access code.
    failed_attempts: Mapped[int] = mapped_column(default=0)
    banned_until: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Who THIS person is, in their own words — work, goals, interests, constraints.
    # Injected into the prompt so the assistant fits them, not a hardcoded persona.
    profile: Mapped[Optional[str]] = mapped_column(Text)

    # Encrypted Google OAuth token (JSON), set after the user connects Google.
    google_token_enc: Mapped[Optional[str]] = mapped_column(Text)
    google_email: Mapped[Optional[str]] = mapped_column(String(255))
    # IDs of the per-user Google resources the bot maintains.
    sheet_id: Mapped[Optional[str]] = mapped_column(String(255))
    drive_folder_id: Mapped[Optional[str]] = mapped_column(String(255))

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AppSetting(Base):
    """Global key/value settings the owner can change from Telegram (e.g. OAuth creds)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class GoogleAccount(Base):
    """One linked personal Google account (OAuth). A user may link several."""
    __tablename__ = "google_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)     # encrypted OAuth token
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MailAccount(Base):
    """A generic IMAP/SMTP mailbox (e.g. Migadu). Password stored encrypted."""
    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[str] = mapped_column(Text, nullable=False)
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(default=993)
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(default=465)
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConnectedSheet(Base):
    """A Google Sheet the user shared with the bot. A user may connect several."""
    __tablename__ = "connected_sheets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    sheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OAuthState(Base):
    """Short-lived random token linking a Google login redirect back to a user.

    Prevents CSRF and tells us *who* authorised when Google calls our callback.
    """
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    """A logged money event (basic data-entry / accountant work)."""
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    kind: Mapped[str] = mapped_column(String(20))          # "in" / "out"
    category: Mapped[Optional[str]] = mapped_column(String(80))
    note: Mapped[Optional[str]] = mapped_column(Text)
    # Exact words the user typed — audit trail so nothing is silently mis-recorded.
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="transactions")


class Reminder(Base):
    """A time-based nudge. The scheduler fires it via Telegram at due_at."""
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)  # UTC
    fired: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # None = one-off. Otherwise it re-arms itself after firing:
    # daily | weekdays | weekends | weekly
    repeat: Mapped[Optional[str]] = mapped_column(String(12))


class Task(Base):
    """An open piece of work. Unlike a Reminder it has no required time — it sits
    in the list until it's done, which is what makes 'what's pending?' answerable."""
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # open | done | dropped
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    # 1 = urgent, 2 = normal, 3 = whenever
    priority: Mapped[int] = mapped_column(default=2)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)  # UTC, optional
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # --- Plan structure: a task can hold other tasks, so a whole roadmap fits here.
    parent_id: Mapped[Optional[int]] = mapped_column(index=True)
    # track = the big area (dsa, dev, life...), phase = a stage inside it,
    # task = a single job, habit = something that repeats.
    kind: Mapped[str] = mapped_column(String(10), default="task", index=True)
    track: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    order_idx: Mapped[int] = mapped_column(default=0)
    # What actually counts as finished — the phase's gate, in the user's words.
    gate: Mapped[Optional[str]] = mapped_column(Text)
    # Countable work: 12 of 45 problems solved.
    target: Mapped[Optional[int]] = mapped_column()
    progress: Mapped[int] = mapped_column(default=0)
    # Habits: "daily" / "weekly" / "4x_week", with a streak.
    recur: Mapped[Optional[str]] = mapped_column(String(16))
    last_done_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    streak: Mapped[int] = mapped_column(default=0)


class ConversationTurn(Base):
    """One message in a user's chat history, kept so context survives restarts.

    Fully isolated: loaded only by telegram_id, never across users.
    """
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(12))       # "user" / "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Secret(Base):
    """An encrypted vault entry (password/note). secret_enc is Fernet-encrypted."""
    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)   # "gmail", "wifi"
    username: Mapped[Optional[str]] = mapped_column(String(255))
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MemoryVector(Base):
    """One embedded piece of this user's history, for meaning-based recall.

    Keyword search misses "what did I decide about caching" when the message
    said "Redis cache-aside". The vector doesn't.
    """
    __tablename__ = "memory_vectors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), default="turn", index=True)  # turn | fact | plan
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON array of floats. SQLite has no vector type and a few thousand short
    # vectors scan in milliseconds, so a real vector DB would be overkill here.
    vector: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ActionLog(Base):
    """An undo point: what changed, and the state of the plan just before it.

    Snapshot-based on purpose — one implementation reverses every tool that
    touches the plan, instead of hand-writing an inverse for each one and
    getting a few of them subtly wrong.
    """
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    tool: Mapped[str] = mapped_column(String(40))
    summary: Mapped[Optional[str]] = mapped_column(Text)     # human-readable, for "what changed?"
    snapshot: Mapped[str] = mapped_column(Text)              # JSON: plan + reminders + profile BEFORE
    undone: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Account(Base):
    """A bill / card the user tracks: when its statement arrives and when it's due."""
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)   # "HDFC Credit Card"
    statement_day: Mapped[Optional[int]] = mapped_column()           # day of month 1-31
    due_day: Mapped[Optional[int]] = mapped_column()                 # day of month 1-31
    # Gmail search to find this account's statement email, e.g. "from:hdfc statement".
    statement_query: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
_engine = create_engine(config.DATABASE_URL, echo=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _migrate_add_columns()
    _migrate_single_sheet()


def _migrate_single_sheet() -> None:
    """Move any legacy single User.sheet_id into the connected_sheets table."""
    with session() as s:
        users = s.scalars(select(User).where(User.sheet_id.is_not(None))).all()
        for u in users:
            has = s.scalars(select(ConnectedSheet).where(
                ConnectedSheet.telegram_id == u.telegram_id,
                ConnectedSheet.sheet_id == u.sheet_id)).first()
            if not has:
                s.add(ConnectedSheet(telegram_id=u.telegram_id, sheet_id=u.sheet_id,
                                     title="My Sheet", is_default=True))
        s.commit()


def _migrate_add_columns() -> None:
    """Safely add any new nullable columns to existing SQLite tables (no data loss)."""
    from sqlalchemy import inspect, text
    wanted = {
        "transactions": [("raw_text", "TEXT")],
        "reminders": [("repeat", "VARCHAR(12)")],
        "tasks": [("parent_id", "INTEGER"), ("kind", "VARCHAR(10) DEFAULT 'task'"),
                  ("track", "VARCHAR(40)"), ("order_idx", "INTEGER DEFAULT 0"),
                  ("gate", "TEXT"), ("target", "INTEGER"),
                  ("progress", "INTEGER DEFAULT 0"), ("recur", "VARCHAR(16)"),
                  ("last_done_at", "DATETIME"), ("streak", "INTEGER DEFAULT 0")],
        "users": [("verified", "INTEGER DEFAULT 0"), ("custom_sa_enc", "TEXT"),
                  ("custom_oauth_enc", "TEXT"), ("default_account", "TEXT"),
                  ("failed_attempts", "INTEGER DEFAULT 0"), ("banned_until", "DATETIME"),
                  ("profile", "TEXT")],
    }
    insp = inspect(_engine)
    with _engine.begin() as conn:
        for table, cols in wanted.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, sqltype in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}"))


def session() -> Session:
    return Session(_engine)


# --- User helpers ---------------------------------------------------------
def get_or_create_user(telegram_id: int, name: str | None = None) -> User:
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, name=name)
            s.add(user)
            s.commit()
            s.refresh(user)
        elif name and not user.name:
            user.name = name
            s.commit()
        s.expunge(user)
        return user


# --- Transaction helpers --------------------------------------------------
def last_transaction(telegram_id: int) -> "Transaction | None":
    with session() as s:
        row = s.scalars(
            select(Transaction)
            .where(Transaction.telegram_id == telegram_id)
            .order_by(Transaction.id.desc())
        ).first()
        if row:
            s.expunge(row)
        return row


def delete_transaction(telegram_id: int, tx_id: int) -> "Transaction | None":
    with session() as s:
        row = s.get(Transaction, tx_id)
        if row is None or row.telegram_id != telegram_id:   # ownership check
            return None
        # Detached copy so the caller can show what was removed.
        data = Transaction(id=row.id, telegram_id=row.telegram_id, amount=row.amount,
                           kind=row.kind, category=row.category, note=row.note)
        s.delete(row)
        s.commit()
        return data


def update_transaction(
    telegram_id: int, tx_id: int, amount: float | None = None,
    kind: str | None = None, category: str | None = None, note: str | None = None,
) -> bool:
    with session() as s:
        row = s.get(Transaction, tx_id)
        if row is None or row.telegram_id != telegram_id:
            return False
        if amount is not None:
            row.amount = amount
        if kind is not None:
            row.kind = kind
        if category is not None:
            row.category = category
        if note is not None:
            row.note = note
        s.commit()
        return True


# --- Google connection helpers -------------------------------------------
def save_google_connection(
    telegram_id: int, token_json_enc: str, email: str
) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            s.add(user)
        user.google_token_enc = token_json_enc
        user.google_email = email
        s.commit()


def get_google_token_enc(telegram_id: int) -> str | None:
    with session() as s:
        user = s.get(User, telegram_id)
        return user.google_token_enc if user else None


def get_google_email(telegram_id: int) -> str | None:
    with session() as s:
        user = s.get(User, telegram_id)
        return user.google_email if user else None


# --- Global app settings --------------------------------------------------
def get_setting(key: str) -> str | None:
    with session() as s:
        row = s.get(AppSetting, key)
        return row.value if row else None


def set_setting(key: str, value: str) -> None:
    with session() as s:
        row = s.get(AppSetting, key)
        if row:
            row.value = value
        else:
            s.add(AppSetting(key=key, value=value))
        s.commit()


# --- Multiple linked Google accounts (OAuth) -----------------------------
def add_google_account(telegram_id: int, email: str, token_enc: str) -> None:
    """Add or update a linked Google account (keyed by email)."""
    with session() as s:
        existing = s.scalars(
            select(GoogleAccount).where(
                GoogleAccount.telegram_id == telegram_id, GoogleAccount.email == email
            )
        ).first()
        if existing:
            existing.token_enc = token_enc
        else:
            s.add(GoogleAccount(telegram_id=telegram_id, email=email, token_enc=token_enc))
        s.commit()


def list_google_accounts(telegram_id: int) -> list["GoogleAccount"]:
    with session() as s:
        rows = s.scalars(
            select(GoogleAccount).where(GoogleAccount.telegram_id == telegram_id)
            .order_by(GoogleAccount.id)
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_account_token_enc(telegram_id: int, email: str) -> str | None:
    with session() as s:
        row = s.scalars(
            select(GoogleAccount).where(
                GoogleAccount.telegram_id == telegram_id, GoogleAccount.email == email
            )
        ).first()
        return row.token_enc if row else None


def set_default_account(telegram_id: int, email: str | None) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if user:
            user.default_account = email
            s.commit()


def get_default_account(telegram_id: int) -> str | None:
    with session() as s:
        user = s.get(User, telegram_id)
        return user.default_account if user else None


def add_mail_account(telegram_id: int, email: str, password_enc: str,
                     imap_host: str, imap_port: int, smtp_host: str, smtp_port: int) -> int:
    """Add/update an IMAP mailbox. First one becomes default. Returns total count."""
    with session() as s:
        row = s.scalars(select(MailAccount).where(
            MailAccount.telegram_id == telegram_id, MailAccount.email == email)).first()
        if row:
            row.password_enc = password_enc
            row.imap_host, row.imap_port = imap_host, imap_port
            row.smtp_host, row.smtp_port = smtp_host, smtp_port
        else:
            any_m = s.scalars(select(MailAccount).where(
                MailAccount.telegram_id == telegram_id)).first()
            s.add(MailAccount(telegram_id=telegram_id, email=email, password_enc=password_enc,
                              imap_host=imap_host, imap_port=imap_port,
                              smtp_host=smtp_host, smtp_port=smtp_port,
                              is_default=(any_m is None)))
        s.commit()
        return s.scalar(select(func.count()).select_from(MailAccount).where(
            MailAccount.telegram_id == telegram_id))


def list_mail_accounts(telegram_id: int) -> list["MailAccount"]:
    with session() as s:
        rows = s.scalars(select(MailAccount).where(
            MailAccount.telegram_id == telegram_id).order_by(MailAccount.id)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_mail_account(telegram_id: int, email: str | None = None) -> "MailAccount | None":
    with session() as s:
        stmt = select(MailAccount).where(MailAccount.telegram_id == telegram_id)
        if email:
            stmt = stmt.where(MailAccount.email.ilike(f"%{email}%"))
        else:
            dflt = s.scalars(stmt.where(MailAccount.is_default.is_(True))).first()
            if dflt:
                s.expunge(dflt)
                return dflt
        row = s.scalars(stmt.order_by(MailAccount.id)).first()
        if row:
            s.expunge(row)
        return row


def set_default_mail(telegram_id: int, email: str) -> bool:
    with session() as s:
        rows = s.scalars(select(MailAccount).where(
            MailAccount.telegram_id == telegram_id)).all()
        found = False
        for r in rows:
            r.is_default = email.lower() in r.email.lower()
            found = found or r.is_default
        s.commit()
        return found


def remove_mail_account(telegram_id: int, email: str) -> bool:
    with session() as s:
        row = s.scalars(select(MailAccount).where(
            MailAccount.telegram_id == telegram_id,
            MailAccount.email.ilike(f"%{email}%"))).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def remove_google_account(telegram_id: int, email: str) -> bool:
    with session() as s:
        row = s.scalars(
            select(GoogleAccount).where(
                GoogleAccount.telegram_id == telegram_id,
                GoogleAccount.email.ilike(f"%{email}%"),
            )
        ).first()
        if not row:
            return False
        s.delete(row)
        s.commit()
        return True


def get_user_name(telegram_id: int) -> str | None:
    with session() as s:
        user = s.get(User, telegram_id)
        return user.name if user else None


# --- Verification + custom service account --------------------------------
def is_verified(telegram_id: int) -> bool:
    with session() as s:
        user = s.get(User, telegram_id)
        return bool(user and user.verified)


def set_verified(telegram_id: int, name: str | None = None) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, name=name)
            s.add(user)
        user.verified = True
        if name and not user.name:
            user.name = name
        s.commit()


def set_custom_sa(telegram_id: int, enc: str | None) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            s.add(user)
        user.custom_sa_enc = enc
        s.commit()


def get_custom_sa_enc(telegram_id: int) -> str | None:
    with session() as s:
        user = s.get(User, telegram_id)
        return user.custom_sa_enc if user else None


def set_custom_oauth(telegram_id: int, enc: str | None) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            s.add(user)
        user.custom_oauth_enc = enc
        s.commit()


def get_custom_oauth_enc(telegram_id: int) -> str | None:
    with session() as s:
        user = s.get(User, telegram_id)
        return user.custom_oauth_enc if user else None


# --- Brute-force / ban helpers -------------------------------------------
def banned_seconds_left(telegram_id: int) -> int:
    """Seconds remaining on a ban, or 0 if not banned."""
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None or user.banned_until is None:
            return 0
        left = (user.banned_until - datetime.utcnow()).total_seconds()
        return int(left) if left > 0 else 0


def record_failed_code(telegram_id: int, name: str | None, max_attempts: int,
                       ban_hours: int) -> tuple[int, bool]:
    """Increment failed attempts. Returns (attempts_used, is_now_banned)."""
    from datetime import timedelta
    with session() as s:
        user = s.get(User, telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, name=name)
            s.add(user)
        user.failed_attempts = (user.failed_attempts or 0) + 1
        banned = False
        if user.failed_attempts >= max_attempts:
            user.banned_until = datetime.utcnow() + timedelta(hours=ban_hours)
            user.failed_attempts = 0
            banned = True
        s.commit()
        return (max_attempts if banned else user.failed_attempts), banned


def reset_failed_code(telegram_id: int) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if user:
            user.failed_attempts = 0
            user.banned_until = None
            s.commit()


def set_user_resources(
    telegram_id: int, sheet_id: str | None = None, folder_id: str | None = None
) -> None:
    with session() as s:
        user = s.get(User, telegram_id)
        if sheet_id is not None:
            user.sheet_id = sheet_id
        if folder_id is not None:
            user.drive_folder_id = folder_id
        s.commit()


# --- OAuth state helpers --------------------------------------------------
def create_oauth_state(telegram_id: int) -> str:
    import secrets
    state = secrets.token_urlsafe(32)
    with session() as s:
        s.add(OAuthState(state=state, telegram_id=telegram_id))
        s.commit()
    return state


def consume_oauth_state(state: str, max_age_seconds: int = 900) -> int | None:
    """Return the telegram_id for a valid, unexpired state, then delete it."""
    with session() as s:
        row = s.get(OAuthState, state)
        if row is None:
            return None
        age = (datetime.utcnow() - row.created_at).total_seconds()
        tid = row.telegram_id
        s.delete(row)
        s.commit()
        return tid if age <= max_age_seconds else None


# --- Account helpers ------------------------------------------------------
def add_account(
    telegram_id: int, name: str, statement_day: int | None = None,
    due_day: int | None = None, statement_query: str | None = None,
) -> int:
    with session() as s:
        acc = Account(
            telegram_id=telegram_id, name=name, statement_day=statement_day,
            due_day=due_day, statement_query=statement_query,
        )
        s.add(acc)
        s.commit()
        return acc.id


def list_transactions(telegram_id: int, limit: int = 15) -> list["Transaction"]:
    """Most recent transactions first, so they can be listed and corrected by id."""
    with session() as s:
        rows = s.scalars(select(Transaction)
                         .where(Transaction.telegram_id == telegram_id)
                         .order_by(Transaction.id.desc()).limit(limit)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def update_reminder(telegram_id: int, reminder_id: int, text: str | None = None,
                    due_at: datetime | None = None,
                    repeat: str | None = None, clear_repeat: bool = False):
    with session() as s:
        r = s.get(Reminder, reminder_id)
        if r is None or r.telegram_id != telegram_id:   # ownership check
            return None
        if text is not None:
            r.text = text
        if due_at is not None:
            r.due_at = due_at
            r.fired = False
        if clear_repeat:
            r.repeat = None
        elif repeat is not None:
            r.repeat = repeat
        s.commit()
        s.refresh(r)
        s.expunge(r)
        return r


def find_bill(telegram_id: int, name: str) -> "Account | None":
    n = (name or "").strip().lower()
    for a in list_accounts(telegram_id):
        if n and n in (a.name or "").lower():
            return a
    return None


def update_bill(telegram_id: int, name: str, new_name: str | None = None,
                statement_day: int | None = None, due_day: int | None = None):
    with session() as s:
        acc = s.scalars(select(Account).where(
            Account.telegram_id == telegram_id)).all()
        target = next((a for a in acc if name.lower() in (a.name or "").lower()), None)
        if target is None:
            return None
        if new_name:
            target.name = new_name
        if statement_day is not None:
            target.statement_day = statement_day
        if due_day is not None:
            target.due_day = due_day
        s.commit()
        s.refresh(target)
        s.expunge(target)
        return target


def delete_bill(telegram_id: int, name: str) -> str | None:
    with session() as s:
        acc = s.scalars(select(Account).where(
            Account.telegram_id == telegram_id)).all()
        target = next((a for a in acc if name.lower() in (a.name or "").lower()), None)
        if target is None:
            return None
        label = target.name
        s.delete(target)
        s.commit()
        return label


def list_accounts(telegram_id: int) -> list[Account]:
    with session() as s:
        rows = s.scalars(
            select(Account).where(Account.telegram_id == telegram_id)
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def accounts_due_soon(today_day: int, window_days: int) -> list[Account]:
    """Accounts whose due_day is between today and today+window (this month)."""
    with session() as s:
        rows = s.scalars(select(Account).where(Account.due_day.is_not(None))).all()
        out = []
        for a in rows:
            diff = a.due_day - today_day
            if 0 <= diff <= window_days:
                s.expunge(a)
                out.append(a)
        return out


def all_users_with_google() -> list[User]:
    """Every connected user — used by the scheduler to sweep all accounts."""
    with session() as s:
        rows = s.scalars(
            select(User).where(User.google_token_enc.is_not(None))
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_user_resources(telegram_id: int) -> tuple[str | None, str | None]:
    """Return (default_sheet_id, drive_folder_id) for a user."""
    with session() as s:
        user = s.get(User, telegram_id)
        folder = user.drive_folder_id if user else None
    return default_sheet_id(telegram_id), folder


# --- Multiple connected sheets -------------------------------------------
def add_sheet(telegram_id: int, sheet_id: str, title: str | None) -> int:
    """Add/refresh a connected sheet. First one becomes the default. Returns total count."""
    with session() as s:
        existing = s.scalars(select(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id,
            ConnectedSheet.sheet_id == sheet_id)).first()
        if existing:
            existing.title = title
        else:
            any_sheet = s.scalars(select(ConnectedSheet).where(
                ConnectedSheet.telegram_id == telegram_id)).first()
            s.add(ConnectedSheet(telegram_id=telegram_id, sheet_id=sheet_id,
                                 title=title, is_default=(any_sheet is None)))
        s.commit()
        return s.scalar(select(func.count()).select_from(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id))


def list_sheets(telegram_id: int) -> list["ConnectedSheet"]:
    with session() as s:
        rows = s.scalars(select(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id).order_by(ConnectedSheet.id)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def count_sheets(telegram_id: int) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id)) or 0


def default_sheet_id(telegram_id: int) -> str | None:
    with session() as s:
        row = s.scalars(select(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id,
            ConnectedSheet.is_default.is_(True))).first()
        if row is None:  # fall back to the first sheet if no default flagged
            row = s.scalars(select(ConnectedSheet).where(
                ConnectedSheet.telegram_id == telegram_id).order_by(ConnectedSheet.id)).first()
        return row.sheet_id if row else None


def set_default_sheet(telegram_id: int, sheet_id: str) -> bool:
    with session() as s:
        rows = s.scalars(select(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id)).all()
        found = False
        for r in rows:
            r.is_default = (r.sheet_id == sheet_id)
            found = found or r.is_default
        s.commit()
        return found


def resolve_sheet(telegram_id: int, hint: str) -> str | None:
    """Find a connected sheet by a partial title match."""
    with session() as s:
        rows = s.scalars(select(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id)).all()
        for r in rows:
            if hint.lower() in (r.title or "").lower():
                return r.sheet_id
    return None


def remove_sheet(telegram_id: int, sheet_id: str) -> bool:
    with session() as s:
        row = s.scalars(select(ConnectedSheet).where(
            ConnectedSheet.telegram_id == telegram_id,
            ConnectedSheet.sheet_id == sheet_id)).first()
        if row is None:
            return False
        was_default = row.is_default
        s.delete(row)
        s.commit()
        if was_default:  # promote another to default
            nxt = s.scalars(select(ConnectedSheet).where(
                ConnectedSheet.telegram_id == telegram_id).order_by(ConnectedSheet.id)).first()
            if nxt:
                nxt.is_default = True
                s.commit()
        return True


# --- Reminder helpers -----------------------------------------------------
# --- Tasks ---------------------------------------------------------------
def add_task(telegram_id: int, title: str, notes: str | None = None,
             priority: int = 2, due_at: datetime | None = None) -> int:
    with session() as s:
        t = Task(telegram_id=telegram_id, title=title.strip(), notes=notes,
                 priority=max(1, min(int(priority or 2), 3)), due_at=due_at)
        s.add(t)
        s.commit()
        return t.id


def list_tasks(telegram_id: int, status: str = "open", limit: int = 100,
               due_before: datetime | None = None) -> list["Task"]:
    """Open tasks come back urgent-first, then soonest due, then oldest."""
    with session() as s:
        stmt = select(Task).where(Task.telegram_id == telegram_id)
        if status != "all":
            stmt = stmt.where(Task.status == status)
        if due_before is not None:
            stmt = stmt.where(Task.due_at.is_not(None), Task.due_at <= due_before)
        if status == "done":
            stmt = stmt.order_by(Task.done_at.desc())
        else:
            stmt = stmt.order_by(Task.priority, Task.due_at.is_(None), Task.due_at, Task.id)
        rows = s.scalars(stmt.limit(limit)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def count_open_tasks(telegram_id: int) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(Task).where(
            Task.telegram_id == telegram_id, Task.status == "open")) or 0


def get_task(telegram_id: int, task_id: int) -> "Task | None":
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.telegram_id != telegram_id:   # ownership check
            return None
        s.expunge(t)
        return t


def set_task_status(telegram_id: int, task_id: int, status: str) -> "Task | None":
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.telegram_id != telegram_id:   # ownership check
            return None
        t.status = status
        t.done_at = datetime.utcnow() if status in ("done", "dropped") else None
        s.commit()
        s.refresh(t)          # commit expires the attributes — reload before detaching
        s.expunge(t)
        return t


def update_task(telegram_id: int, task_id: int, title: str | None = None,
                notes: str | None = None, priority: int | None = None,
                due_at: datetime | None = None, clear_due: bool = False,
                gate: str | None = None, target: int | None = None,
                progress: int | None = None, recur: str | None = None,
                status: str | None = None) -> "Task | None":
    """Change any editable field of a task or plan node. Only what's passed moves.

    The plan fields (gate/target/progress/recur/status) are here too, so a phase
    can be corrected in place instead of the whole track being re-sent.
    """
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.telegram_id != telegram_id:   # ownership check
            return None
        if title is not None:
            t.title = title.strip()
        if notes is not None:
            t.notes = notes
        if priority is not None:
            t.priority = max(1, min(int(priority), 3))
        if clear_due:
            t.due_at = None
        elif due_at is not None:
            t.due_at = due_at
        if gate is not None:
            t.gate = gate.strip() or None
        if target is not None:
            t.target = max(0, int(target)) or None
        if progress is not None:
            t.progress = max(0, int(progress))
        if recur is not None:
            t.recur = recur.strip() or None
        if status in ("open", "done", "dropped"):
            t.status = status
            t.done_at = datetime.utcnow() if status == "done" else None
        s.commit()
        s.refresh(t)          # commit expires the attributes — reload before detaching
        s.expunge(t)
        return t


def find_tasks(telegram_id: int, text: str, status: str = "open") -> list["Task"]:
    """Match open tasks by a word or two from their title — for 'mark X done'."""
    needle = (text or "").strip().lower()
    if not needle:
        return []
    return [t for t in list_tasks(telegram_id, status=status, limit=200)
            if needle in (t.title or "").lower()]


def users_with_open_tasks() -> list[int]:
    """Telegram ids that have at least one open task (for the daily digest)."""
    with session() as s:
        return list(s.scalars(select(Task.telegram_id).where(
            Task.status == "open").distinct()).all())


def get_profile(telegram_id: int) -> str | None:
    with session() as s:
        u = s.get(User, telegram_id)
        return u.profile if u else None


def set_profile(telegram_id: int, text: str | None) -> None:
    with session() as s:
        u = s.get(User, telegram_id)
        if u:
            u.profile = (text or "").strip() or None
            s.commit()


# --- Plan tree -----------------------------------------------------------
def add_node(telegram_id: int, title: str, kind: str = "task",
             parent_id: int | None = None, track: str | None = None,
             notes: str | None = None, gate: str | None = None,
             priority: int = 2, target: int | None = None,
             recur: str | None = None, order_idx: int = 0,
             due_at: datetime | None = None) -> int:
    """One node of a plan. A phase is just a task that holds other tasks."""
    with session() as s:
        t = Task(telegram_id=telegram_id, title=title.strip(), kind=kind,
                 parent_id=parent_id, track=track, notes=notes, gate=gate,
                 priority=max(1, min(int(priority or 2), 3)), target=target,
                 recur=recur, order_idx=order_idx, due_at=due_at)
        s.add(t)
        s.commit()
        return t.id


def children(telegram_id: int, parent_id: int | None, status: str = "all") -> list["Task"]:
    with session() as s:
        stmt = select(Task).where(Task.telegram_id == telegram_id,
                                  Task.parent_id.is_(None) if parent_id is None
                                  else Task.parent_id == parent_id)
        if status != "all":
            stmt = stmt.where(Task.status == status)
        rows = s.scalars(stmt.order_by(Task.order_idx, Task.id)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def tracks(telegram_id: int) -> list["Task"]:
    """Top-level nodes — the big areas of the plan."""
    return children(telegram_id, None)


def subtree_stats(telegram_id: int, node_id: int) -> tuple[int, int]:
    """(done_leaves, total_leaves) beneath a node — how far a phase really is."""
    kids = children(telegram_id, node_id)
    if not kids:
        return (0, 0)
    done = total = 0
    for k in kids:
        if k.status == "dropped":
            continue
        d, t = subtree_stats(telegram_id, k.id)
        if t == 0:                       # a leaf
            total += 1
            done += 1 if k.status == "done" else 0
        else:
            done += d
            total += t
    return done, total


def bump_progress(telegram_id: int, task_id: int, by: int = 1) -> "Task | None":
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.telegram_id != telegram_id:   # ownership check
            return None
        t.progress = max(0, (t.progress or 0) + by)
        if t.target and t.progress >= t.target and t.status == "open":
            t.status = "done"
            t.done_at = datetime.utcnow()
        s.commit()
        s.refresh(t)
        s.expunge(t)
        return t


def touch_habit(telegram_id: int, task_id: int, today_utc: datetime) -> "Task | None":
    """Check a habit off for today and keep the streak honest."""
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.telegram_id != telegram_id:   # ownership check
            return None
        last = t.last_done_at
        gap = (today_utc.date() - last.date()).days if last else None
        if gap == 0:                     # already done today — no double count
            s.expunge(t)
            return t
        t.streak = (t.streak or 0) + 1 if gap is not None and gap <= 2 else 1
        t.last_done_at = today_utc
        t.progress = (t.progress or 0) + 1
        s.commit()
        s.refresh(t)
        s.expunge(t)
        return t


def habits(telegram_id: int) -> list["Task"]:
    with session() as s:
        rows = s.scalars(select(Task).where(
            Task.telegram_id == telegram_id, Task.kind == "habit",
            Task.status == "open").order_by(Task.order_idx, Task.id)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def open_leaves(telegram_id: int, track: str | None = None, limit: int = 200) -> list["Task"]:
    """Open, actionable items in plan order.

    A leaf is anything with no children — including a phase that is itself the
    work (e.g. "P1 Arrays — 45 problems"). Tracks and habits are never leaves.
    """
    with session() as s:
        stmt = select(Task).where(Task.telegram_id == telegram_id,
                                  Task.status == "open",
                                  Task.kind.notin_(("track", "habit")))
        if track:
            stmt = stmt.where(Task.track == track)
        rows = list(s.scalars(stmt.order_by(Task.order_idx, Task.id)).all())
        parents = {r.parent_id for r in s.scalars(select(Task).where(
            Task.telegram_id == telegram_id, Task.parent_id.is_not(None))).all()}
        leaves = [r for r in rows if r.id not in parents][:limit]
        for r in leaves:
            s.expunge(r)
        return leaves


# --- Semantic memory --------------------------------------------------------
_MEM_FIELDS = ("id", "kind", "text", "vector", "created_at")


def add_memory(telegram_id: int, kind: str, text: str, vector_json: str) -> int:
    with session() as s:
        m = MemoryVector(telegram_id=telegram_id, kind=kind, text=text,
                         vector=vector_json)
        s.add(m)
        s.commit()
        return m.id


def memory_exists(telegram_id: int, text: str) -> bool:
    """Don't embed the same line twice (re-sent messages, retries)."""
    with session() as s:
        return s.scalar(select(MemoryVector.id).where(
            MemoryVector.telegram_id == telegram_id,
            MemoryVector.text == text).limit(1)) is not None


def all_memories(telegram_id: int, limit: int = 4000) -> list[dict]:
    """This user's vectors only — never anyone else's."""
    with session() as s:
        rows = s.scalars(select(MemoryVector).where(
            MemoryVector.telegram_id == telegram_id).order_by(
            MemoryVector.id.desc()).limit(limit)).all()
        return [{"id": r.id, "kind": r.kind, "text": r.text,
                 "vector": r.vector, "when": r.created_at} for r in rows]


def count_memories(telegram_id: int) -> int:
    with session() as s:
        return len(list(s.scalars(select(MemoryVector.id).where(
            MemoryVector.telegram_id == telegram_id)).all()))


# --- Undo journal -----------------------------------------------------------
def snapshot_user(telegram_id: int) -> dict:
    """Everything reversible for this user: plan tree, reminders, profile.

    Money is deliberately excluded — transactions already have their own undo,
    and silently rolling accounting back inside a plan undo would be dangerous.
    """
    with session() as s:
        tasks = s.scalars(select(Task).where(
            Task.telegram_id == telegram_id)).all()
        rems = s.scalars(select(Reminder).where(
            Reminder.telegram_id == telegram_id)).all()
        u = s.get(User, telegram_id)
        return {
            "tasks": [{c.name: _iso(getattr(t, c.name))
                       for c in Task.__table__.columns} for t in tasks],
            "reminders": [{c.name: _iso(getattr(r, c.name))
                           for c in Reminder.__table__.columns} for r in rems],
            "profile": u.profile if u else None,
        }


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _unwrap(model, row: dict) -> dict:
    """Turn stored ISO strings back into datetimes for the columns that need it."""
    out = {}
    for c in model.__table__.columns:
        v = row.get(c.name)
        if isinstance(v, str) and isinstance(c.type, DateTime):
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                v = None
        out[c.name] = v
    return out


def restore_user(telegram_id: int, snap: dict) -> tuple[int, int]:
    """Put the plan + reminders + profile back exactly as the snapshot had them.

    Rows are re-inserted with their ORIGINAL ids, so parent_id links inside the
    plan tree still point at the right nodes after a restore.
    """
    with session() as s:
        for row in s.scalars(select(Task).where(
                Task.telegram_id == telegram_id)).all():
            s.delete(row)
        for row in s.scalars(select(Reminder).where(
                Reminder.telegram_id == telegram_id)).all():
            s.delete(row)
        s.flush()
        for row in snap.get("tasks") or []:
            s.add(Task(**_unwrap(Task, row)))
        for row in snap.get("reminders") or []:
            s.add(Reminder(**_unwrap(Reminder, row)))
        u = s.get(User, telegram_id)
        if u:
            u.profile = snap.get("profile")
        s.commit()
        return len(snap.get("tasks") or []), len(snap.get("reminders") or [])


def log_action(telegram_id: int, tool: str, summary: str, snapshot_json: str,
               keep: int = 20) -> int:
    """Record an undo point and trim the oldest beyond `keep`."""
    with session() as s:
        a = ActionLog(telegram_id=telegram_id, tool=tool, summary=summary,
                      snapshot=snapshot_json)
        s.add(a)
        s.commit()
        new_id = a.id
        old = s.scalars(select(ActionLog).where(
            ActionLog.telegram_id == telegram_id).order_by(
            ActionLog.id.desc()).offset(max(1, keep))).all()
        for row in old:
            s.delete(row)
        s.commit()
        return new_id


def recent_actions(telegram_id: int, limit: int = 10,
                   only_undoable: bool = False) -> list["ActionLog"]:
    with session() as s:
        stmt = select(ActionLog).where(ActionLog.telegram_id == telegram_id)
        if only_undoable:
            stmt = stmt.where(ActionLog.undone == False)   # noqa: E712
        rows = s.scalars(stmt.order_by(ActionLog.id.desc()).limit(limit)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def mark_undone(telegram_id: int, action_id: int) -> bool:
    with session() as s:
        a = s.get(ActionLog, action_id)
        if a is None or a.telegram_id != telegram_id:      # ownership check
            return False
        a.undone = True
        s.commit()
        return True


def find_nodes(telegram_id: int, text: str, kinds: tuple[str, ...] | None = None,
               include_done: bool = True) -> list["Task"]:
    """Match ANY plan node by words from its title — phases and habits included.

    find_tasks() only looks at open leaf work, so a finished phase or a track was
    impossible to point at. This is what "edit the DSA P3 gate" resolves through.
    """
    needle = (text or "").strip().lower()
    if not needle:
        return []
    with session() as s:
        stmt = select(Task).where(Task.telegram_id == telegram_id)
        if kinds:
            stmt = stmt.where(Task.kind.in_(kinds))
        if not include_done:
            stmt = stmt.where(Task.status == "open")
        rows = list(s.scalars(stmt.order_by(Task.order_idx, Task.id)).all())
        hits = [r for r in rows if needle in (r.title or "").lower()]
        # Nothing matched whole — try every word, so "P3 gate" finds "P3 Stack".
        if not hits:
            words = [w for w in needle.split() if len(w) > 1]
            hits = [r for r in rows
                    if words and all(w in (r.title or "").lower() for w in words)]
        for r in hits:
            s.expunge(r)
        return hits


def next_order_idx(telegram_id: int, parent_id: int | None) -> int:
    """Where a newly appended child belongs — after the ones already there."""
    kids = children(telegram_id, parent_id)
    return (max((k.order_idx or 0) for k in kids) + 1) if kids else 0


def delete_subtree(telegram_id: int, node_id: int) -> int:
    """Delete a node and everything under it. Returns how many rows went."""
    kids = children(telegram_id, node_id)
    gone = 0
    for k in kids:
        gone += delete_subtree(telegram_id, k.id)
    with session() as s:
        t = s.get(Task, node_id)
        if t is not None and t.telegram_id == telegram_id:   # ownership check
            s.delete(t)
            s.commit()
            gone += 1
    return gone


def delete_all_reminders(telegram_id: int) -> int:
    """Drop every reminder for this user (their own rows only)."""
    with session() as s:
        rows = s.scalars(select(Reminder).where(Reminder.telegram_id == telegram_id)).all()
        n = len(rows)
        for r in rows:
            s.delete(r)
        s.commit()
        return n


def delete_all_tasks(telegram_id: int) -> int:
    """Wipe this user's whole plan/task list. Only ever their own rows."""
    with session() as s:
        rows = s.scalars(select(Task).where(Task.telegram_id == telegram_id)).all()
        n = len(rows)
        for r in rows:
            s.delete(r)
        s.commit()
        return n


def find_tracks(telegram_id: int, name: str) -> list["Task"]:
    """Top-level tracks whose title matches — used to replace or drop one."""
    n = (name or "").strip().lower()
    return [t for t in tracks(telegram_id) if n and n in (t.title or "").lower()]


def add_reminder(telegram_id: int, text: str, due_at: datetime,
                 repeat: str | None = None) -> int:
    with session() as s:
        r = Reminder(telegram_id=telegram_id, text=text, due_at=due_at, repeat=repeat)
        s.add(r)
        s.commit()
        return r.id


def list_reminders(telegram_id: int, include_fired: bool = False) -> list[Reminder]:
    with session() as s:
        stmt = select(Reminder).where(Reminder.telegram_id == telegram_id)
        if not include_fired:
            stmt = stmt.where(Reminder.fired.is_(False))
        rows = s.scalars(stmt.order_by(Reminder.due_at)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def cancel_reminder(telegram_id: int, reminder_id: int) -> bool:
    with session() as s:
        r = s.get(Reminder, reminder_id)
        if r is None or r.telegram_id != telegram_id:   # ownership check
            return False
        s.delete(r)
        s.commit()
        return True


def due_reminders(now_utc: datetime) -> list[Reminder]:
    """All unfired reminders whose time has arrived (across all users)."""
    with session() as s:
        rows = s.scalars(
            select(Reminder).where(
                Reminder.fired.is_(False), Reminder.due_at <= now_utc
            )
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def _next_occurrence(due: datetime, repeat: str) -> datetime:
    """Next time this repeating reminder should fire, keeping the time of day."""
    from datetime import timedelta
    nxt = due + timedelta(weeks=1) if repeat == "weekly" else due + timedelta(days=1)
    if repeat == "weekdays":
        while nxt.weekday() >= 5:            # skip Sat/Sun
            nxt += timedelta(days=1)
    elif repeat == "weekends":
        while nxt.weekday() < 5:
            nxt += timedelta(days=1)
    return nxt


def mark_reminder_fired(reminder_id: int) -> None:
    """One-off reminders are done; repeating ones re-arm for their next slot."""
    from datetime import timedelta
    with session() as s:
        r = s.get(Reminder, reminder_id)
        if not r:
            return
        if r.repeat:
            nxt = _next_occurrence(r.due_at, r.repeat)
            now = datetime.utcnow()
            while nxt <= now:                # catch up after downtime
                nxt = _next_occurrence(nxt, r.repeat)
            r.due_at = nxt
            r.fired = False
        else:
            r.fired = True
        s.commit()


# --- Secret (vault) helpers ----------------------------------------------
def save_secret(telegram_id: int, name: str, secret_enc: str, username: str | None) -> None:
    with session() as s:
        # Upsert by (telegram_id, name).
        existing = s.scalars(
            select(Secret).where(
                Secret.telegram_id == telegram_id, Secret.name == name
            )
        ).first()
        if existing:
            existing.secret_enc = secret_enc
            existing.username = username
        else:
            s.add(Secret(telegram_id=telegram_id, name=name,
                         secret_enc=secret_enc, username=username))
        s.commit()


def get_secret(telegram_id: int, name: str) -> Secret | None:
    with session() as s:
        row = s.scalars(
            select(Secret).where(
                Secret.telegram_id == telegram_id, Secret.name.ilike(name)
            )
        ).first()
        if row:
            s.expunge(row)
        return row


def list_secret_names(telegram_id: int) -> list[str]:
    with session() as s:
        rows = s.scalars(
            select(Secret.name).where(Secret.telegram_id == telegram_id)
        ).all()
        return list(rows)


def delete_secret(telegram_id: int, name: str) -> bool:
    with session() as s:
        row = s.scalars(
            select(Secret).where(
                Secret.telegram_id == telegram_id, Secret.name.ilike(name)
            )
        ).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


# --- Conversation memory helpers -----------------------------------------
def save_turn(telegram_id: int, role: str, content: str) -> None:
    with session() as s:
        s.add(ConversationTurn(telegram_id=telegram_id, role=role, content=content))
        s.commit()


def search_turns(telegram_id: int, needle: str, limit: int = 12) -> list[dict]:
    """Find past messages containing `needle` — recall beyond the recent window."""
    needle = (needle or "").strip()
    if not needle:
        return []
    with session() as s:
        rows = s.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.telegram_id == telegram_id,
                   ConversationTurn.content.ilike(f"%{needle}%"))
            .order_by(ConversationTurn.id.desc())
            .limit(limit)
        ).all()
    return [{"role": r.role, "content": r.content,
             "when": r.created_at.strftime("%d %b %H:%M") if r.created_at else ""}
            for r in reversed(rows)]


def recent_turns(telegram_id: int, limit: int = 12) -> list[dict]:
    """Last `limit` turns for this user, oldest-first, as [{'role','content'}]."""
    with session() as s:
        rows = s.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.telegram_id == telegram_id)
            .order_by(ConversationTurn.id.desc())
            .limit(limit)
        ).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]

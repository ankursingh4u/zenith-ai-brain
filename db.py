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
        "users": [("verified", "INTEGER DEFAULT 0"), ("custom_sa_enc", "TEXT"),
                  ("custom_oauth_enc", "TEXT"), ("default_account", "TEXT"),
                  ("failed_attempts", "INTEGER DEFAULT 0"), ("banned_until", "DATETIME")],
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
def add_reminder(telegram_id: int, text: str, due_at: datetime) -> int:
    with session() as s:
        r = Reminder(telegram_id=telegram_id, text=text, due_at=due_at)
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


def mark_reminder_fired(reminder_id: int) -> None:
    with session() as s:
        r = s.get(Reminder, reminder_id)
        if r:
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

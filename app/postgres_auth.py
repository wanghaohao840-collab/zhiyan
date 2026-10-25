"""Authentication against the explicitly migrated PostgreSQL users table."""

from __future__ import annotations

import uuid
from datetime import datetime

from psycopg.errors import UniqueViolation

from app.auth import (AuthError, User, hash_password, normalize_username, utc_now,
                      validate_password, validate_username, verify_password)
from app.postgres import PostgresDatabase


def _insert_new_user_in_transaction(cursor, *, user_id: str, username: str,
                                    username_key: str, password_hash: str,
                                    now: str) -> User:
    """Insert one prepared active account on the caller's cursor."""
    if (type(user_id) is not str or str(uuid.UUID(user_id)) != user_id):
        raise ValueError("Invalid prepared user ID")
    display, key = validate_username(username)
    if display != username or key != username_key:
        raise ValueError("Invalid prepared username")
    if (type(password_hash) is not str or not password_hash.startswith("scrypt$v=1$")
            or type(now) is not str):
        raise ValueError("Invalid prepared account fields")
    timestamp = datetime.fromisoformat(now)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Prepared timestamp must be timezone-aware")
    cursor.execute(
        "insert into users (id,username,username_key,password_hash,status,created_at,updated_at) "
        "values (%s,%s,%s,%s,'active',%s,%s)",
        (user_id, display, key, password_hash, now, now),
    )
    return User(id=user_id, username=display)


class PostgresAuthService:
    def __init__(self, database: PostgresDatabase):
        self.database = database

    def register(self, username: str, password: str) -> User:
        display, key = validate_username(username)
        validate_password(password)
        user_id = str(uuid.uuid4())
        now = utc_now()
        password_hash = hash_password(password)
        try:
            with self.database.transaction() as cursor:
                return _insert_new_user_in_transaction(
                    cursor, user_id=user_id, username=display, username_key=key,
                    password_hash=password_hash, now=now,
                )
        except UniqueViolation as exc:
            if exc.diag.constraint_name != "users_username_key_key":
                raise
            raise AuthError("Username already exists") from exc

    def authenticate(self, username: str, password: str) -> User:
        _, key = normalize_username(username)
        with self.database.transaction() as cursor:
            row = cursor.execute(
                "select id,username,password_hash from users "
                "where username_key=%s and status='active'", (key,),
            ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            raise AuthError("Invalid username or password")
        return User(id=row["id"], username=row["username"])

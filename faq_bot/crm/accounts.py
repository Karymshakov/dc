"""Учётные записи CRM: администратор и операторы.

Роли:

    admin     создаёт операторов, задаёт и сбрасывает им пароли, отключает;
              одобряет предложения в базу, отменяет выученные фразы,
              выгружает обучение
    operator  входит под своим логином и работает с диалогами и очередью
              обучения

Пароли хранятся только как PBKDF2-SHA256 с солью, открытый текст нигде
не сохраняется. Первый администратор создаётся при пустой базе:
логин `admin`, пароль из `CRM_ADMIN_PASSWORD` или `admin123!`. Этот
пароль стоит сменить сразу после первого входа.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Any

ADMIN = "admin"
OPERATOR = "operator"
ROLES = (ADMIN, OPERATOR)

DEFAULT_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123!"

ITERATIONS = 200_000
MIN_PASSWORD = 6
LOGIN_RE = re.compile(r"^[a-z0-9._-]{3,32}$")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(actual.hex(), digest)


#: Хэш-пустышка: вход с несуществующим логином тратит то же время, что и
#: с существующим, и не выдаёт, какие логины заведены.
_DUMMY = hash_password(secrets.token_hex(8))


def normalize_login(login: str) -> str:
    return (login or "").strip().lower()


def validate(login: str, name: str, password: str | None, role: str) -> None:
    if not LOGIN_RE.match(login):
        raise ValueError("логин: 3-32 символа, латиница, цифры, точка, дефис, подчёркивание")
    if not name.strip() or len(name.strip()) > 40:
        raise ValueError("имя: от 1 до 40 символов")
    if password is not None and len(password) < MIN_PASSWORD:
        raise ValueError(f"пароль: не короче {MIN_PASSWORD} символов")
    if role not in ROLES:
        raise ValueError("роль: admin или operator")


def public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Учётная запись без хэша пароля — то, что можно отдать клиенту."""
    if row is None:
        return None
    return {key: value for key, value in row.items() if key != "password_hash"}


class Accounts:
    """Операции с учётными записями поверх Store."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def ensure_admin(self) -> bool:
        """Создать первого администратора, если администраторов нет."""
        if self.store.scalar("SELECT COUNT(*) FROM operators WHERE role = 'admin'"):
            return False
        password = os.environ.get("CRM_ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD
        self.store.add_operator(DEFAULT_ADMIN_LOGIN, "Администратор", ADMIN,
                                hash_password(password))
        return True

    def authenticate(self, login: str, password: str) -> dict[str, Any] | None:
        row = self.store.operator_by_login(normalize_login(login))
        if row is None:
            check_password(password, _DUMMY)
            return None
        if not check_password(password, row["password_hash"]) or not row["active"]:
            return None
        return public(row)

    def create(self, login: str, name: str, password: str,
               role: str = OPERATOR) -> dict[str, Any]:
        login = normalize_login(login)
        validate(login, name, password, role)
        if self.store.operator_by_login(login) is not None:
            raise ValueError("такой логин уже есть")
        operator_id = self.store.add_operator(login, name.strip(), role,
                                              hash_password(password))
        return public(self.store.operator(operator_id))

    def update(self, operator_id: int, actor_id: int, *, name: str | None = None,
               role: str | None = None, active: bool | None = None,
               password: str | None = None) -> dict[str, Any]:
        row = self.store.operator(operator_id)
        if row is None:
            raise KeyError(operator_id)
        fields: dict[str, Any] = {}
        if name is not None:
            validate(row["login"], name, None, row["role"])
            fields["name"] = name.strip()
        if role is not None:
            validate(row["login"], row["name"], None, role)
            fields["role"] = role
        if active is not None:
            fields["active"] = 1 if active else 0
        if password is not None:
            validate(row["login"], row["name"], password, row["role"])
            fields["password_hash"] = hash_password(password)
        if operator_id == actor_id and (fields.get("active") == 0
                                        or fields.get("role") == OPERATOR):
            raise ValueError("нельзя отключить себя или снять с себя роль администратора")
        if (row["role"] == ADMIN and (fields.get("active") == 0
                                      or fields.get("role") == OPERATOR)
                and self._admins_left(operator_id) == 0):
            raise ValueError("должен остаться хотя бы один активный администратор")
        if fields:
            self.store.update_operator(operator_id, **fields)
        return public(self.store.operator(operator_id))

    def _admins_left(self, excluding: int) -> int:
        return int(self.store.scalar(
            "SELECT COUNT(*) FROM operators WHERE role = 'admin' AND active = 1"
            " AND id != ?", (int(excluding),)) or 0)

    def list(self) -> list[dict[str, Any]]:
        return [public(row) for row in self.store.operators()]

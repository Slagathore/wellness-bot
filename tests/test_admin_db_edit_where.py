"""Guards for the db_edit WHERE parser.

The endpoint previously interpolated an admin-supplied WHERE string straight
into SQL (rejecting only ``;``). ``_parse_pk_where`` now restricts it to a
parameterized ``<pk> = <id>`` / ``<pk> IN (...)`` form.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.interfaces.admin.server import _parse_pk_where


def test_pk_equality_is_parameterized() -> None:
    clause, params = _parse_pk_where("id", "id = 42")
    assert clause == "id = ?"
    assert params == [42]


def test_pk_in_list_is_parameterized() -> None:
    clause, params = _parse_pk_where("id", "id IN (1, 2, 3)")
    assert clause == "id IN (?, ?, ?)"
    assert params == [1, 2, 3]


@pytest.mark.parametrize(
    "malicious",
    [
        "1=1",
        "id = 1 OR 1=1",
        "id = 1 UNION SELECT password FROM users",
        "id = 1 --",
        "name = 'x'",
        "id = 1; DROP TABLE users",
        "id LIKE '%'",
        "other_col = 5",  # not the primary key
        "",
    ],
)
def test_injection_and_non_pk_clauses_rejected(malicious: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_pk_where("id", malicious)
    assert exc.value.status_code == 400

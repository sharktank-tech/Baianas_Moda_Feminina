"""FASE 9.0.0-H — contrato de isolamento do schema pollomodas."""

from migrations.validate_0021 import ROLES, validate_schema


class FakeCursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return next(self.rows)


class FakeConnection:
    def __init__(self, rows):
        self.cursor_instance = FakeCursor(rows)

    def cursor(self):
        return self.cursor_instance


def _clean_rows():
    rows = []
    for _ in ROLES:
        rows.extend([(False, False), (0,), (0,), (0,)])
    return rows


def test_private_schema_validator_accepts_no_privileges():
    conn = FakeConnection(_clean_rows())
    assert validate_schema(conn) == []


def test_private_schema_validator_reports_schema_access():
    rows = _clean_rows()
    rows[0] = (True, False)
    errors = validate_schema(FakeConnection(rows))
    assert "anon has schema privileges" in errors


def test_private_schema_validator_reports_relation_access():
    rows = _clean_rows()
    rows[1] = (1,)
    errors = validate_schema(FakeConnection(rows))
    assert "anon has relation privileges" in errors


def test_private_schema_validator_checks_all_api_roles():
    conn = FakeConnection(_clean_rows())
    validate_schema(conn)
    params = [
        params
        for query, params in conn.cursor_instance.calls
        if "has_schema_privilege" in query
    ]
    assert params == [(role, role) for role in ROLES]

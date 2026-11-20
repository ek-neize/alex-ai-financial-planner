"""Unit tests for the migration runner, using a fake Data API client (no AWS needed)."""

import pytest

from run_migrations import (
    MIGRATIONS_DIR,
    DataApi,
    Migration,
    MigrationError,
    discover_migrations,
    migrate,
    pending_migrations,
    split_statements,
)


# --------------------------------------------------------------------------
# split_statements
# --------------------------------------------------------------------------


def test_split_simple_statements_and_comments():
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE a (id INT);  -- trailing comment
    CREATE TABLE b (id INT);
    """
    assert split_statements(sql) == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]


def test_split_keeps_semicolons_inside_strings_and_dollar_quotes():
    sql = """
    INSERT INTO t VALUES ('a;b', 'it''s; fine');
    CREATE FUNCTION f() RETURNS TRIGGER AS $$
    BEGIN
        NEW.x = 1;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    DO $body$ BEGIN PERFORM 1; END $body$;
    """
    statements = split_statements(sql)
    assert len(statements) == 3
    assert statements[0] == "INSERT INTO t VALUES ('a;b', 'it''s; fine')"
    assert "NEW.x = 1;" in statements[1] and statements[1].endswith("LANGUAGE plpgsql")
    assert statements[2] == "DO $body$ BEGIN PERFORM 1; END $body$"


def test_split_rejects_unterminated_dollar_quote():
    with pytest.raises(MigrationError):
        split_statements("CREATE FUNCTION f() AS $$ BEGIN")


def test_real_schema_migration_splits_cleanly():
    statements = split_statements((MIGRATIONS_DIR / "001_schema.sql").read_text())
    assert len(statements) == 17
    assert statements[0].startswith("CREATE EXTENSION")
    # Re-runnable on a database created by the old, untracked runner
    assert all("CREATE TRIGGER" not in s for s in statements)


# --------------------------------------------------------------------------
# discover_migrations / pending_migrations
# --------------------------------------------------------------------------


def test_discover_orders_numerically(tmp_path):
    for name in ["010_later.sql", "002_second.sql", "001_first.sql"]:
        (tmp_path / name).write_text("SELECT 1;")
    assert [m.version for m in discover_migrations(tmp_path)] == ["001_first", "002_second", "010_later"]


@pytest.mark.parametrize("names", [["001_a.sql", "001_b.sql"], ["1-bad name.sql"]])
def test_discover_rejects_bad_or_duplicate_names(tmp_path, names):
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_pending_skips_applied():
    a, b = Migration("001_a", "SELECT 1"), Migration("002_b", "SELECT 2")
    assert pending_migrations([a, b], {"001_a": a.checksum}) == [b]


def test_pending_rejects_edited_migration():
    a = Migration("001_a", "SELECT 1")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        pending_migrations([a], {"001_a": Migration("001_a", "SELECT 'old'").checksum})


def test_pending_rejects_deleted_migration():
    with pytest.raises(MigrationError, match="missing"):
        pending_migrations([], {"001_a": "x" * 64})


# --------------------------------------------------------------------------
# migrate, against a fake Data API with transaction semantics
# --------------------------------------------------------------------------


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeDataApi:
    """Records committed statements; rolled-back transactions leave no trace."""

    def __init__(self, fail_on=None, resuming_calls=0):
        self.committed = []  # statements, in commit order
        self.tracking = {}  # version -> checksum (the schema_migrations table)
        self.transactions = {}
        self.fail_on = fail_on
        self.resuming_calls = resuming_calls

    def begin_transaction(self, **_):
        tid = f"tx{len(self.transactions)}"
        self.transactions[tid] = []
        return {"transactionId": tid}

    def execute_statement(self, sql, transactionId=None, parameters=None, **_):
        if self.resuming_calls:
            self.resuming_calls -= 1
            raise FakeClientError("DatabaseResumingException")
        if self.fail_on and self.fail_on in sql:
            raise FakeClientError("BadRequestException")
        if sql.startswith("SELECT version, checksum"):
            return {"records": [[{"stringValue": v}, {"stringValue": c}] for v, c in self.tracking.items()]}
        if sql.startswith("INSERT INTO schema_migrations"):
            values = {p["name"]: p["value"]["stringValue"] for p in parameters}
            self.transactions[transactionId].append(("track", values))
            return {}
        if transactionId:
            self.transactions[transactionId].append(("sql", sql))
        else:
            self.committed.append(sql)
        return {}

    def commit_transaction(self, transactionId, **_):
        for kind, item in self.transactions.pop(transactionId):
            if kind == "track":
                self.tracking[item["version"]] = item["checksum"]
            else:
                self.committed.append(item)

    def rollback_transaction(self, transactionId, **_):
        self.transactions.pop(transactionId)


def make_db(fake):
    return DataApi(fake, "arn:aws:rds:us-east-1:123:cluster:test", "arn:secret", "alex")


MIGRATIONS = [
    Migration("001_init", "CREATE TABLE a (id INT); CREATE TABLE b (id INT);"),
    Migration("002_more", "ALTER TABLE a ADD COLUMN x INT;"),
]


def test_migrate_applies_all_in_order_and_records_them():
    fake = FakeDataApi()
    assert migrate(make_db(fake), MIGRATIONS) == 2
    user_statements = [s for s in fake.committed if "schema_migrations" not in s]
    assert user_statements == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)", "ALTER TABLE a ADD COLUMN x INT"]
    assert fake.tracking == {m.version: m.checksum for m in MIGRATIONS}


def test_migrate_is_idempotent():
    fake = FakeDataApi()
    migrate(make_db(fake), MIGRATIONS)
    committed_before = list(fake.committed)
    assert migrate(make_db(fake), MIGRATIONS) == 0
    assert [s for s in fake.committed if s not in committed_before] == []


def test_migrate_only_applies_new_migrations():
    fake = FakeDataApi()
    migrate(make_db(fake), MIGRATIONS[:1])
    assert migrate(make_db(fake), MIGRATIONS) == 1
    assert fake.committed[-1] == "ALTER TABLE a ADD COLUMN x INT"


def test_failed_migration_rolls_back_entirely():
    fake = FakeDataApi(fail_on="ALTER TABLE")
    with pytest.raises(FakeClientError):
        migrate(make_db(fake), MIGRATIONS)
    # 001 committed; nothing from 002 (statements or tracking row) was kept
    assert "002_more" not in fake.tracking
    assert "001_init" in fake.tracking
    assert not any("ALTER TABLE" in s for s in fake.committed)


def test_status_only_changes_nothing():
    fake = FakeDataApi()
    assert migrate(make_db(fake), MIGRATIONS, status_only=True) == 0
    assert fake.tracking == {}


def test_retries_while_database_resumes(monkeypatch):
    monkeypatch.setattr("run_migrations.time.sleep", lambda _: None)
    fake = FakeDataApi(resuming_calls=2)
    assert migrate(make_db(fake), MIGRATIONS) == 2


def test_retries_while_new_cluster_is_not_visible_to_data_api(monkeypatch):
    # Seen live: right after creation, the Data API says "Cannot find DBInstance in DBCluster"
    monkeypatch.setattr("run_migrations.time.sleep", lambda _: None)
    fake = FakeDataApi()
    calls = {"n": 0}
    original = fake.execute_statement

    def not_ready_twice(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise FakeClientError("DatabaseNotFoundException")
        return original(**kwargs)

    fake.execute_statement = not_ready_twice
    assert migrate(make_db(fake), MIGRATIONS) == 2


def test_gives_up_after_bounded_retries(monkeypatch):
    monkeypatch.setattr("run_migrations.time.sleep", lambda _: None)
    fake = FakeDataApi(resuming_calls=100)
    with pytest.raises(FakeClientError):
        migrate(make_db(fake), MIGRATIONS)

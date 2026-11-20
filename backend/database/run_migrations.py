#!/usr/bin/env python3
"""
Versioned database migration runner (Aurora Data API).

Applies migrations/NNN_name.sql files in order and records each applied file
in a schema_migrations table, so every migration runs exactly once per
database:

- Each migration runs in a single transaction: it applies completely or not at all.
- An applied migration whose file has since changed is an error. Write a new
  migration instead of editing an old one.
- Exits non-zero on any failure, so Terraform and CI can rely on it.

Usage:
    cd backend/database
    uv run run_migrations.py            # apply pending migrations
    uv run run_migrations.py --status   # show applied/pending, change nothing

Run automatically by terraform/5_database (on cluster creation and whenever a
migration changes) and by the CI/CD pipeline before deploying the agents.
"""

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_FILE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(255) PRIMARY KEY,
    checksum    CHAR(64) NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT NOW()
)"""


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: str  # file name without .sql, e.g. "001_schema"
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return all migrations in directory, ordered by their numeric prefix."""
    migrations = []
    for path in directory.glob("*.sql"):
        match = MIGRATION_FILE.match(path.name)
        if not match:
            raise MigrationError(f"Bad migration file name {path.name!r}: expected NNN_lower_snake.sql")
        migrations.append((int(match.group(1)), Migration(path.stem, path.read_text(encoding="utf-8"))))

    numbers = [n for n, _ in migrations]
    duplicates = {n for n in numbers if numbers.count(n) > 1}
    if duplicates:
        raise MigrationError(f"Duplicate migration numbers: {sorted(duplicates)}")

    return [m for _, m in sorted(migrations, key=lambda item: item[0])]


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements (the Data API runs one at a time).

    Splits on semicolons, ignoring those inside -- comments, quoted strings and
    dollar-quoted bodies ($$ ... $$ or $tag$ ... $tag$) such as PL/pgSQL functions.
    """
    statements, current = [], []
    i, n = 0, len(sql)
    dollar_tag = None  # active $tag$ while inside a dollar-quoted body

    while i < n:
        if dollar_tag:
            if sql.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                current.append(sql[i])
                i += 1
            continue

        ch = sql[i]
        if sql.startswith("--", i):  # line comment: skip to end of line
            end = sql.find("\n", i)
            i = n if end == -1 else end
        elif ch in ("'", '"'):  # quoted string or identifier ('' escapes a quote)
            end = i + 1
            while end < n:
                if sql[end] == ch:
                    if end + 1 < n and sql[end + 1] == ch:
                        end += 2
                        continue
                    break
                end += 1
            current.append(sql[i : end + 1])
            i = end + 1
        elif ch == "$" and (tag := re.match(r"\$[A-Za-z_]*\$", sql[i:])):
            dollar_tag = tag.group(0)
            current.append(dollar_tag)
            i += len(dollar_tag)
        elif ch == ";":
            statements.append("".join(current))
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1

    if dollar_tag:
        raise MigrationError(f"Unterminated dollar-quoted block {dollar_tag}")
    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


def pending_migrations(migrations: list[Migration], applied: dict[str, str]) -> list[Migration]:
    """Return migrations not yet applied. `applied` maps version -> checksum.

    Fails if an applied migration's file was changed or deleted, since the
    database no longer matches the code.
    """
    known = {m.version for m in migrations}
    missing = sorted(set(applied) - known)
    if missing:
        raise MigrationError(f"Applied migrations missing from {MIGRATIONS_DIR.name}/: {missing}")

    for m in migrations:
        if m.version in applied and applied[m.version] != m.checksum:
            raise MigrationError(
                f"{m.version}.sql changed after it was applied. "
                "Never edit an applied migration; add a new one instead."
            )
    return [m for m in migrations if m.version not in applied]


class DataApi:
    """Thin wrapper over the RDS Data API client."""

    # Aurora can briefly reject calls while the cluster starts up or resumes
    RETRYABLE = ("DatabaseResumingException", "ServiceUnavailableError")

    def __init__(self, client, cluster_arn: str, secret_arn: str, database: str):
        self.client = client
        self.target = dict(resourceArn=cluster_arn, secretArn=secret_arn, database=database)

    def _call(self, method: str, **kwargs):
        for attempt in range(1, 11):
            try:
                return getattr(self.client, method)(**self.target, **kwargs)
            except Exception as e:  # botocore ClientError, or a fake client's error in tests
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if code in self.RETRYABLE and attempt < 10:
                    print(f"    ⏳ {code}, retrying in {attempt * 3}s...")
                    time.sleep(attempt * 3)
                    continue
                raise

    def execute(self, sql: str, transaction_id: str | None = None, parameters=None):
        kwargs = {"sql": sql}
        if transaction_id:
            kwargs["transactionId"] = transaction_id
        if parameters:
            kwargs["parameters"] = parameters
        return self._call("execute_statement", **kwargs)

    def begin(self) -> str:
        return self._call("begin_transaction")["transactionId"]

    def commit(self, transaction_id: str):
        self.client.commit_transaction(resourceArn=self.target["resourceArn"],
                                       secretArn=self.target["secretArn"], transactionId=transaction_id)

    def rollback(self, transaction_id: str):
        self.client.rollback_transaction(resourceArn=self.target["resourceArn"],
                                         secretArn=self.target["secretArn"], transactionId=transaction_id)


def applied_migrations(db: DataApi) -> dict[str, str]:
    db.execute(CREATE_TRACKING_TABLE)
    result = db.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    return {row[0]["stringValue"]: row[1]["stringValue"] for row in result.get("records", [])}


def apply_migration(db: DataApi, migration: Migration):
    """Run all statements of one migration and record it, in one transaction."""
    statements = split_statements(migration.sql)
    transaction_id = db.begin()
    try:
        for stmt in statements:
            db.execute(stmt, transaction_id)
        db.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES (:version, :checksum)",
            transaction_id,
            parameters=[
                {"name": "version", "value": {"stringValue": migration.version}},
                {"name": "checksum", "value": {"stringValue": migration.checksum}},
            ],
        )
    except Exception:
        db.rollback(transaction_id)
        raise
    db.commit(transaction_id)
    return len(statements)


def migrate(db: DataApi, migrations: list[Migration], status_only: bool = False) -> int:
    """Apply pending migrations. Returns how many were applied."""
    applied = applied_migrations(db)
    pending = pending_migrations(migrations, applied)

    for m in migrations:
        print(f"  {'✅ applied' if m.version in applied else '⏳ pending'}  {m.version}")
    if status_only or not pending:
        print("\nNothing to apply." if not pending else f"\n{len(pending)} pending (--status: not applied).")
        return 0

    for m in pending:
        print(f"\n▶️  Applying {m.version}...")
        count = apply_migration(db, m)
        print(f"   ✅ {count} statements committed")
    return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="show applied/pending migrations only")
    args = parser.parse_args()

    import boto3
    from dotenv import load_dotenv

    # Real environment variables (e.g. set by Terraform or CI) win over .env
    load_dotenv(override=False)
    cluster_arn = os.environ.get("AURORA_CLUSTER_ARN")
    secret_arn = os.environ.get("AURORA_SECRET_ARN")
    database = os.environ.get("AURORA_DATABASE", "alex")
    region = os.environ.get("DEFAULT_AWS_REGION", "us-east-1")
    if not cluster_arn or not secret_arn:
        print("❌ Missing AURORA_CLUSTER_ARN or AURORA_SECRET_ARN")
        return 1

    print(f"🚀 Database migrations: {cluster_arn.split(':')[-1]} / {database}")
    print("=" * 50)
    db = DataApi(boto3.client("rds-data", region_name=region), cluster_arn, secret_arn, database)
    try:
        applied = migrate(db, discover_migrations(), status_only=args.status)
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        return 1
    print(f"\n✅ Done ({applied} applied)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

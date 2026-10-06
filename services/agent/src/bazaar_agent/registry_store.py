"""Transactional registry storage. No accounts, model calls or executable artifacts."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import logfire
from bazaar_protocol import ErrorCode
from bazaar_protocol.registry import (
    AgentRecord,
    CreateStrategyRequest,
    CreateVersionRequest,
    LegacyStrategyDefinition,
    Page,
    StrategyDefinition,
    StrategyRecord,
    StrategyRegistration,
    StrategyVersion,
)
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError


class _LegacyCreateStrategyRequest(CreateStrategyRequest):
    """Historical normalization for read-only replay, not a submission model."""

    definition: LegacyStrategyDefinition


class _LegacyCreateVersionRequest(CreateVersionRequest):
    """Historical normalization for read-only replay, not a submission model."""

    definition: LegacyStrategyDefinition


SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_strategies (
    strategy_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL UNIQUE REFERENCES registry_agents(agent_id),
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_versions (
    version_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES registry_strategies(strategy_id),
    version INTEGER NOT NULL CHECK (version > 0),
    definition TEXT NOT NULL,
    parent_version_id TEXT REFERENCES registry_versions(version_id),
    hypothesis TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (strategy_id, version)
);
CREATE TABLE IF NOT EXISTS registry_requests (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY (scope, request_key)
);
"""


class RegistryError(Exception):
    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class RegistryStore:
    def __init__(self, database_path: Path, model_refs: frozenset[str], actor_label: str) -> None:
        self.database_path = database_path
        # Retain the constructor argument for existing API callers. Runtime model
        # catalogs no longer constrain instructions-only strategy registration.
        self.actor_label = actor_label

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection = logfire.instrument_sqlite3(connection)
        connection.cursor().execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @logfire.instrument("registry.initialize", extract_args=False)
    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _replay(
        connection: sqlite3.Connection, scope: str, key: UUID, fingerprint: str
    ) -> str | None:
        row = (
            connection.cursor()
            .execute(
                "SELECT fingerprint, response FROM registry_requests WHERE scope = ? AND request_key = ?",
                (scope, str(key)),
            )
            .fetchone()
        )
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise RegistryError(
                409, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key content differs"
            )
        return row["response"]

    def replay_legacy_request(
        self, payload: object, key: UUID, *, strategy_id: UUID | None = None
    ) -> StrategyRegistration | StrategyVersion | None:
        """Replay a valid historical request only; never create records or consume keys.

        Preserve all legacy fields and the original request normalization when
        comparing fingerprints. Invalid legacy bodies and unused keys are not
        compatible retries; a used key with different valid content conflicts.
        """
        request_type = (
            _LegacyCreateStrategyRequest if strategy_id is None else _LegacyCreateVersionRequest
        )
        try:
            request = request_type.model_validate(payload)
        except ValidationError:
            return None
        scope = "create" if strategy_id is None else str(strategy_id)
        with self.connect() as connection:
            connection.cursor().execute("PRAGMA query_only = ON")
            replay = self._replay(connection, scope, key, digest(request.model_dump(mode="json")))
        if replay is None:
            return None
        response_type = StrategyRegistration if strategy_id is None else StrategyVersion
        result = response_type.model_validate_json(replay)
        logfire.info("Replayed legacy registry request", scope=scope)
        return result

    @staticmethod
    def _remember(
        connection: sqlite3.Connection, scope: str, key: UUID, fingerprint: str, response: str
    ) -> None:
        connection.cursor().execute(
            "INSERT INTO registry_requests VALUES (?, ?, ?, ?)",
            (scope, str(key), fingerprint, response),
        )

    @staticmethod
    def _require_parent(connection: sqlite3.Connection, parent: UUID | None) -> None:
        if parent is not None:
            row = (
                connection.cursor()
                .execute(
                    "SELECT version_id FROM registry_versions WHERE version_id = ?", (str(parent),)
                )
                .fetchone()
            )
            if row is None:
                raise RegistryError(404, ErrorCode.NOT_FOUND, "Parent strategy version not found")

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        strategy_id: UUID,
        number: int,
        definition: StrategyDefinition,
        parent: UUID | None,
        hypothesis: str,
        timestamp: datetime,
    ) -> StrategyVersion:
        version = StrategyVersion(
            version_id=uuid4(),
            strategy_id=strategy_id,
            version=number,
            definition=definition,
            parent_version_id=parent,
            hypothesis=hypothesis,
            definition_digest=digest(definition.model_dump(mode="json")),
            created_at=timestamp,
            created_by=self.actor_label,
        )
        connection.cursor().execute(
            "INSERT INTO registry_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(version.version_id),
                str(strategy_id),
                number,
                definition.model_dump_json(),
                str(parent) if parent else None,
                hypothesis,
                version.definition_digest,
                timestamp.isoformat(),
                self.actor_label,
            ),
        )
        return version

    @logfire.instrument("registry.create", extract_args=False)
    def register(self, request: CreateStrategyRequest, key: UUID) -> StrategyRegistration:
        # Public method callers can supply subclasses or bypass frozen DTO validation.
        # Only replay_legacy_request may interpret historical runtime-bearing inputs.
        # Preserve nested concrete fields during serialization so forged legacy values
        # cannot be silently narrowed to instructions-only by the declared field schema.
        try:
            request = CreateStrategyRequest.model_validate_json(
                request.model_dump_json(serialize_as_any=True)
            )
        except (ValidationError, PydanticSerializationError):
            # Validation details may contain private strategy text; keep telemetry safe.
            raise RegistryError(422, ErrorCode.INVALID_REQUEST, "Invalid request") from None
        fingerprint = digest(request.model_dump(mode="json"))
        with self.connect() as connection:
            connection.cursor().execute("BEGIN IMMEDIATE")
            replay = self._replay(connection, "create", key, fingerprint)
            if replay is not None:
                logfire.info("Replayed strategy registration")
                return StrategyRegistration.model_validate_json(replay)
            self._require_parent(connection, request.parent_version_id)
            if (
                connection.cursor()
                .execute("SELECT agent_id FROM registry_agents WHERE name = ?", (request.name,))
                .fetchone()
            ):
                raise RegistryError(409, ErrorCode.CONFLICT, "Agent name already registered")
            agent_id, strategy_id = uuid4(), uuid4()
            timestamp = datetime.now(UTC)
            connection.cursor().execute(
                "INSERT INTO registry_agents VALUES (?, ?, ?, ?)",
                (str(agent_id), request.name, timestamp.isoformat(), self.actor_label),
            )
            connection.cursor().execute(
                "INSERT INTO registry_strategies VALUES (?, ?, ?, ?, ?)",
                (
                    str(strategy_id),
                    str(agent_id),
                    request.description,
                    timestamp.isoformat(),
                    self.actor_label,
                ),
            )
            version = self._insert_version(
                connection,
                strategy_id,
                1,
                request.definition,
                request.parent_version_id,
                "",
                timestamp,
            )
            result = StrategyRegistration(
                agent=AgentRecord(
                    agent_id=agent_id,
                    name=request.name,
                    strategy_id=strategy_id,
                    created_at=timestamp,
                    created_by=self.actor_label,
                ),
                strategy=StrategyRecord(
                    strategy_id=strategy_id,
                    agent_id=agent_id,
                    description=request.description,
                    created_at=timestamp,
                    created_by=self.actor_label,
                    latest_version_id=version.version_id,
                    version_count=1,
                ),
                version=version,
            )
            self._remember(connection, "create", key, fingerprint, result.model_dump_json())
            logfire.info(
                "Registered named strategy",
                agent_id=str(agent_id),
                strategy_id=str(strategy_id),
                version_id=str(version.version_id),
                agent_name=request.name,
            )
            return result

    @logfire.instrument("registry.add_version", extract_args=False)
    def add_version(
        self, strategy_id: UUID, request: CreateVersionRequest, key: UUID
    ) -> StrategyVersion:
        try:
            request = CreateVersionRequest.model_validate_json(
                request.model_dump_json(serialize_as_any=True)
            )
        except (ValidationError, PydanticSerializationError):
            raise RegistryError(422, ErrorCode.INVALID_REQUEST, "Invalid request") from None
        fingerprint = digest(request.model_dump(mode="json"))
        with self.connect() as connection:
            connection.cursor().execute("BEGIN IMMEDIATE")
            replay = self._replay(connection, str(strategy_id), key, fingerprint)
            if replay is not None:
                logfire.info("Replayed strategy version", strategy_id=str(strategy_id))
                return StrategyVersion.model_validate_json(replay)
            strategy = self._strategy(connection, strategy_id)
            parent = request.parent_version_id or strategy.latest_version_id
            self._require_parent(connection, parent)
            result = self._insert_version(
                connection,
                strategy_id,
                strategy.version_count + 1,
                request.definition,
                parent,
                request.hypothesis,
                datetime.now(UTC),
            )
            self._remember(connection, str(strategy_id), key, fingerprint, result.model_dump_json())
            logfire.info(
                "Registered strategy version",
                strategy_id=str(strategy_id),
                version_id=str(result.version_id),
                version=result.version,
            )
            return result

    @staticmethod
    def _agent(row: sqlite3.Row) -> AgentRecord:
        return AgentRecord.model_validate(dict(row))

    @staticmethod
    def _version(row: sqlite3.Row) -> StrategyVersion:
        return StrategyVersion.model_validate(
            dict(row) | {"definition": json.loads(row["definition"])}
        )

    @staticmethod
    def _strategy(connection: sqlite3.Connection, strategy_id: UUID) -> StrategyRecord:
        row = (
            connection.cursor()
            .execute(
                """SELECT s.*, (SELECT version_id FROM registry_versions v
               WHERE v.strategy_id = s.strategy_id ORDER BY version DESC LIMIT 1) AS latest_version_id,
               (SELECT COUNT(*) FROM registry_versions v WHERE v.strategy_id = s.strategy_id) AS version_count
               FROM registry_strategies s WHERE s.strategy_id = ?""",
                (str(strategy_id),),
            )
            .fetchone()
        )
        if row is None:
            raise RegistryError(404, ErrorCode.NOT_FOUND, "Strategy not found")
        return StrategyRecord.model_validate(dict(row))

    @logfire.instrument("registry.get_strategy", extract_args=False)
    def get_strategy(self, strategy_id: UUID) -> StrategyRecord:
        with self.connect() as connection:
            return self._strategy(connection, strategy_id)

    @logfire.instrument("registry.get_agent", extract_args=False)
    def get_agent(self, agent_id: UUID) -> AgentRecord:
        with self.connect() as connection:
            row = (
                connection.cursor()
                .execute(
                    """SELECT a.*, s.strategy_id FROM registry_agents a JOIN registry_strategies s
                   ON a.agent_id = s.agent_id WHERE a.agent_id = ?""",
                    (str(agent_id),),
                )
                .fetchone()
            )
            if row is None:
                raise RegistryError(404, ErrorCode.NOT_FOUND, "Agent not found")
            return self._agent(row)

    @logfire.instrument("registry.get_version", extract_args=False)
    def get_version(self, strategy_id: UUID, version_id: UUID) -> StrategyVersion:
        with self.connect() as connection:
            self._strategy(connection, strategy_id)
            row = (
                connection.cursor()
                .execute(
                    "SELECT * FROM registry_versions WHERE strategy_id = ? AND version_id = ?",
                    (str(strategy_id), str(version_id)),
                )
                .fetchone()
            )
            if row is None:
                raise RegistryError(404, ErrorCode.NOT_FOUND, "Strategy version not found")
            return self._version(row)

    @logfire.instrument("registry.list_agents", extract_args=False)
    def list_agents(self, limit: int, offset: int) -> Page[AgentRecord]:
        with self.connect() as connection:
            total = (
                connection.cursor().execute("SELECT COUNT(*) FROM registry_agents").fetchone()[0]
            )
            rows = (
                connection.cursor()
                .execute(
                    """SELECT a.*, s.strategy_id FROM registry_agents a JOIN registry_strategies s
                   ON a.agent_id = s.agent_id ORDER BY a.created_at, a.agent_id LIMIT ? OFFSET ?""",
                    (limit, offset),
                )
                .fetchall()
            )
            return Page(
                items=tuple(self._agent(row) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    @logfire.instrument("registry.list_strategies", extract_args=False)
    def list_strategies(self, limit: int, offset: int) -> Page[StrategyRecord]:
        with self.connect() as connection:
            total = (
                connection.cursor()
                .execute("SELECT COUNT(*) FROM registry_strategies")
                .fetchone()[0]
            )
            rows = (
                connection.cursor()
                .execute(
                    "SELECT strategy_id FROM registry_strategies ORDER BY created_at, strategy_id LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                .fetchall()
            )
            return Page(
                items=tuple(self._strategy(connection, UUID(row["strategy_id"])) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    @logfire.instrument("registry.list_versions", extract_args=False)
    def list_versions(self, strategy_id: UUID, limit: int, offset: int) -> Page[StrategyVersion]:
        with self.connect() as connection:
            strategy = self._strategy(connection, strategy_id)
            rows = (
                connection.cursor()
                .execute(
                    "SELECT * FROM registry_versions WHERE strategy_id = ? ORDER BY version LIMIT ? OFFSET ?",
                    (str(strategy_id), limit, offset),
                )
                .fetchall()
            )
            return Page(
                items=tuple(self._version(row) for row in rows),
                total=strategy.version_count,
                limit=limit,
                offset=offset,
            )

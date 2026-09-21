use chrono::{DateTime, TimeZone, Utc};
use memweft_types::{
    CompressionLevel, Episode, Fact, FactStatus, InsightItem, InsightTrigger, InsightType,
    MemoryPacket, Procedure, Scope, ScopeLevel, ValidationState, WorkingState,
};
use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use rusqlite::types::{Type, Value as SqlValue};
use rusqlite::{params_from_iter, Connection};
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::collections::HashSet;
use std::path::{Path, PathBuf};

use crate::{
    EpisodeFilter, Event, EventKind, FactFilter, InsightFilter, StmState, Store, StoreError,
    StoreResult, TimeRangeFilter, WorkingStatePatch,
};

const SCHEMA_VERSION: i64 = 1;

pub struct SqliteStore {
    path: PathBuf,
    pool: Pool<SqliteConnectionManager>,
}

impl std::fmt::Debug for SqliteStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SqliteStore")
            .field("path", &self.path)
            .finish()
    }
}

impl SqliteStore {
    pub fn new<P: Into<PathBuf>>(path: P) -> StoreResult<Self> {
        let path = path.into();
        let as_str = path.to_string_lossy();
        if as_str == ":memory:" {
            return Self::new_in_memory();
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|err| StoreError::Storage(err.to_string()))?;
        }

        // Initialize DB and Schema sequentially before creating the pool
        // to avoid "database is locked" errors when multiple pool connections
        // try to set WAL mode or run migrations simultaneously.
        {
            let mut conn = Connection::open(&path)
                .map_err(|err| StoreError::Storage(err.to_string()))?;
            configure_connection(&mut conn, true)
                .map_err(|err| StoreError::Storage(err.to_string()))?;
            ensure_schema(&conn)?;
        }
        
        let manager = SqliteConnectionManager::file(&path)
            .with_init(|conn| configure_connection(conn, true));
        let pool = Pool::new(manager)
            .map_err(|err| StoreError::Storage(err.to_string()))?;
        
        Ok(Self {
            path,
            pool,
        })
    }

    pub fn new_in_memory() -> StoreResult<Self> {
        let manager = SqliteConnectionManager::memory()
            .with_init(|conn| configure_connection(conn, false));
        // Shared-cache memory databases return SQLITE_LOCKED (not SQLITE_BUSY),
        // so busy_timeout cannot serialize concurrent writers. One connection
        // keeps in-memory semantics deterministic; file stores retain their pool.
        let pool = Pool::builder().max_size(1).build(manager)
            .map_err(|err| StoreError::Storage(err.to_string()))?;
            
        let mut conn = pool.get().map_err(|err| StoreError::Storage(err.to_string()))?;
        ensure_schema(&mut conn)?;
        
        Ok(Self {
            path: PathBuf::from(":memory:"),
            pool,
        })
    }

    pub fn append_events_bulk(&self, events: &[Event]) -> StoreResult<()> {
        if events.is_empty() {
            return Ok(());
        }
        self.with_connection(|conn| {
            let tx = conn.transaction()?;
            let mut stmt_event = tx.prepare(
                "
                INSERT INTO events (
                    event_id, tenant_id, user_id, agent_id, session_id, run_id,
                    ts, kind, payload, tags, entities
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
            )?;
            let mut stmt_tag = tx.prepare(
                "
                INSERT OR IGNORE INTO event_tags (
                    tenant_id, user_id, agent_id, session_id, run_id, event_id, tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ",
            )?;
            let mut stmt_entity = tx.prepare(
                "
                INSERT OR IGNORE INTO event_entities (
                    tenant_id, user_id, agent_id, session_id, run_id, event_id, entity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ",
            )?;
            for event in events {
                stmt_event.execute(params_from_iter(vec![
                    SqlValue::Text(event.event_id.clone()),
                    SqlValue::Text(event.scope.tenant_id.clone()),
                    SqlValue::Text(event.scope.user_id.clone()),
                    SqlValue::Text(event.scope.agent_id.clone()),
                    SqlValue::Text(event.scope.session_id.clone()),
                    SqlValue::Text(event.scope.run_id.clone()),
                    SqlValue::Integer(to_millis(event.ts)),
                    SqlValue::Text(event_kind_to_str(&event.kind).to_string()),
                    SqlValue::Text(encode_json(&event.payload)?),
                    SqlValue::Text(encode_json(&event.tags)?),
                    SqlValue::Text(encode_json(&event.entities)?),
                ]))?;

                for tag in unique_values(&event.tags) {
                    stmt_tag.execute(params_from_iter(vec![
                        SqlValue::Text(event.scope.tenant_id.clone()),
                        SqlValue::Text(event.scope.user_id.clone()),
                        SqlValue::Text(event.scope.agent_id.clone()),
                        SqlValue::Text(event.scope.session_id.clone()),
                        SqlValue::Text(event.scope.run_id.clone()),
                        SqlValue::Text(event.event_id.clone()),
                        SqlValue::Text(tag),
                    ]))?;
                }
                for entity in unique_values(&event.entities) {
                    stmt_entity.execute(params_from_iter(vec![
                        SqlValue::Text(event.scope.tenant_id.clone()),
                        SqlValue::Text(event.scope.user_id.clone()),
                        SqlValue::Text(event.scope.agent_id.clone()),
                        SqlValue::Text(event.scope.session_id.clone()),
                        SqlValue::Text(event.scope.run_id.clone()),
                        SqlValue::Text(event.event_id.clone()),
                        SqlValue::Text(entity),
                    ]))?;
                }
            }
            drop(stmt_entity);
            drop(stmt_tag);
            drop(stmt_event);
            tx.commit()?;
            Ok(())
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn with_connection<F, T>(&self, f: F) -> StoreResult<T>
    where
        F: FnOnce(&mut Connection) -> StoreResult<T>,
    {
        let mut conn = self.pool.get().map_err(|err| StoreError::Storage(err.to_string()))?;
        f(&mut conn)
    }
}

fn configure_connection(conn: &mut Connection, use_wal: bool) -> Result<(), rusqlite::Error> {
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    if use_wal {
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             PRAGMA foreign_keys = ON;",
        )?;
    } else {
        conn.execute_batch(
            "PRAGMA journal_mode = MEMORY;
             PRAGMA synchronous = NORMAL;
             PRAGMA foreign_keys = ON;",
        )?;
    }
    Ok(())
}

fn ensure_schema(conn: &Connection) -> StoreResult<()> {
    crate::documents::ensure_schema(conn)?;
    conn.execute_batch(
        "
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER NOT NULL,
                applied_at INTEGER NOT NULL
            );
            ",
    )?;

    let current = match conn.query_row(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1",
        [],
        |row| row.get::<_, i64>(0),
    ) {
        Ok(version) => version,
        Err(rusqlite::Error::QueryReturnedNoRows) => 0,
        Err(err) => return Err(err.into()),
    };

    if current > SCHEMA_VERSION {
        return Err(StoreError::Storage(format!(
            "database schema version {} is newer than supported {}",
            current, SCHEMA_VERSION
        )));
    }

    if current < SCHEMA_VERSION && current != 0 {
        return Err(StoreError::Storage(format!(
            "database schema version {} requires migration to {}",
            current, SCHEMA_VERSION
        )));
    }

    conn.execute_batch(
        "
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                tags TEXT NOT NULL,
                entities TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_scope_ts
                ON events (tenant_id, user_id, agent_id, session_id, run_id, ts);
            CREATE TABLE IF NOT EXISTS event_tags (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (
                    tenant_id,
                    user_id,
                    agent_id,
                    session_id,
                    run_id,
                    event_id,
                    tag
                )
            );
            CREATE INDEX IF NOT EXISTS event_tags_scope_tag
                ON event_tags (tenant_id, user_id, agent_id, session_id, run_id, tag);
            CREATE INDEX IF NOT EXISTS event_tags_scope_event
                ON event_tags (tenant_id, user_id, agent_id, session_id, run_id, event_id);

            CREATE TABLE IF NOT EXISTS event_entities (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                PRIMARY KEY (
                    tenant_id,
                    user_id,
                    agent_id,
                    session_id,
                    run_id,
                    event_id,
                    entity
                )
            );
            CREATE INDEX IF NOT EXISTS event_entities_scope_entity
                ON event_entities (tenant_id, user_id, agent_id, session_id, run_id, entity);
            CREATE INDEX IF NOT EXISTS event_entities_scope_event
                ON event_entities (tenant_id, user_id, agent_id, session_id, run_id, event_id);

            CREATE TABLE IF NOT EXISTS wm_state (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id)
            );

            CREATE TABLE IF NOT EXISTS stm_state (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                rolling_summary TEXT NOT NULL,
                key_quotes TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, session_id)
            );

            CREATE TABLE IF NOT EXISTS facts (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                fact_id TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                status TEXT NOT NULL,
                valid_from INTEGER,
                valid_to INTEGER,
                confidence REAL NOT NULL,
                sources TEXT NOT NULL,
                scope_level TEXT NOT NULL,
                notes TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, fact_id)
            );
            CREATE INDEX IF NOT EXISTS facts_scope_status
                ON facts (tenant_id, user_id, agent_id, status);

            CREATE TABLE IF NOT EXISTS episodes (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER,
                summary TEXT NOT NULL,
                highlights TEXT NOT NULL,
                tags TEXT NOT NULL,
                entities TEXT NOT NULL,
                sources TEXT NOT NULL,
                compression_level TEXT NOT NULL,
                recency_score REAL,
                PRIMARY KEY (tenant_id, user_id, agent_id, episode_id)
            );
            CREATE INDEX IF NOT EXISTS episodes_scope_start
                ON episodes (tenant_id, user_id, agent_id, start_ts);
            CREATE TABLE IF NOT EXISTS episode_tags (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, episode_id, tag)
            );
            CREATE INDEX IF NOT EXISTS episode_tags_scope_tag
                ON episode_tags (tenant_id, user_id, agent_id, tag);
            CREATE INDEX IF NOT EXISTS episode_tags_scope_episode
                ON episode_tags (tenant_id, user_id, agent_id, episode_id);

            CREATE TABLE IF NOT EXISTS episode_entities (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, episode_id, entity)
            );
            CREATE INDEX IF NOT EXISTS episode_entities_scope_entity
                ON episode_entities (tenant_id, user_id, agent_id, entity);
            CREATE INDEX IF NOT EXISTS episode_entities_scope_episode
                ON episode_entities (tenant_id, user_id, agent_id, episode_id);

            CREATE TABLE IF NOT EXISTS procedures (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                procedure_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                content_json TEXT NOT NULL,
                priority INTEGER NOT NULL,
                sources TEXT NOT NULL,
                applicability TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, procedure_id)
            );
            CREATE INDEX IF NOT EXISTS procedures_scope_task
                ON procedures (tenant_id, user_id, agent_id, task_type);

            CREATE TABLE IF NOT EXISTS insights (
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                insight_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                statement TEXT NOT NULL,
                trigger TEXT NOT NULL,
                confidence REAL NOT NULL,
                validation_state TEXT NOT NULL,
                tests_suggested TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                sources TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id, insight_id)
            );
            CREATE INDEX IF NOT EXISTS insights_scope_state
                ON insights (tenant_id, user_id, agent_id, session_id, run_id, validation_state);

            CREATE TABLE IF NOT EXISTS context_builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                packet_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS context_builds_scope_ts
                ON context_builds (tenant_id, user_id, agent_id, session_id, run_id, ts);
            ",
    )?;

    if current == 0 {
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            params_from_iter(vec![
                SqlValue::Integer(SCHEMA_VERSION),
                SqlValue::Integer(to_millis(Utc::now())),
            ]),
        )?;
    }

    Ok(())
}

impl Store for SqliteStore {
    fn documents(&self, scope: &Scope, prefix: &[String]) -> StoreResult<Vec<crate::Document>> {
        crate::documents::list(self, scope, prefix)
    }

    fn mutate_documents(&self, scope: &Scope, mutations: &[crate::Mutation]) -> StoreResult<()> {
        crate::documents::mutate(self, scope, mutations)
    }

    fn forget_fact(&self, scope: &Scope, key: &str) -> StoreResult<bool> {
        self.with_connection(|conn| {
            let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            crate::documents::forget_derived(&tx, scope, key)?;
            let count = tx.execute(
                "DELETE FROM facts WHERE tenant_id=? AND user_id=? AND agent_id=? AND fact_key=?",
                rusqlite::params![scope.tenant_id, scope.user_id, scope.agent_id, key],
            )?;
            // Persisted context snapshots may contain a copy of the forgotten fact.
            tx.execute("DELETE FROM context_builds WHERE tenant_id=? AND user_id=? AND agent_id=?",
                rusqlite::params![scope.tenant_id, scope.user_id, scope.agent_id])?;
            tx.commit()?;
            Ok(count > 0)
        })
    }
    fn append_event(&self, event: Event) -> StoreResult<()> {
        let Event {
            event_id,
            scope,
            ts,
            kind,
            payload,
            tags,
            entities,
        } = event;
        self.with_connection(|conn| {
            let tx = conn.transaction()?;
            tx.execute(
                "
                INSERT INTO events (
                    event_id, tenant_id, user_id, agent_id, session_id, run_id,
                    ts, kind, payload, tags, entities
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params_from_iter(vec![
                    SqlValue::Text(event_id.clone()),
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(scope.session_id.clone()),
                    SqlValue::Text(scope.run_id.clone()),
                    SqlValue::Integer(to_millis(ts)),
                    SqlValue::Text(event_kind_to_str(&kind).to_string()),
                    SqlValue::Text(encode_json(&payload)?),
                    SqlValue::Text(encode_json(&tags)?),
                    SqlValue::Text(encode_json(&entities)?),
                ]),
            )?;
            insert_event_tags(&tx, &scope, &event_id, &tags, &entities)?;
            tx.commit()?;
            Ok(())
        })
    }

    fn list_events(
        &self,
        scope: &Scope,
        range: TimeRangeFilter,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Event>> {
        self.with_connection(|conn| {
            let mut sql = String::from(
                "SELECT event_id, tenant_id, user_id, agent_id, session_id, run_id, ts, kind, payload, tags, entities
                 FROM events
                 WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?",
            );
            let mut params = scope_params(scope);

            if let Some(start) = range.start {
                sql.push_str(" AND ts >= ?");
                params.push(SqlValue::Integer(to_millis(start)));
            }
            if let Some(end) = range.end {
                sql.push_str(" AND ts <= ?");
                params.push(SqlValue::Integer(to_millis(end)));
            }
            sql.push_str(" ORDER BY ts ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ?");
                params.push(SqlValue::Integer(limit as i64));
            }

            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(params), |row| {
                let kind: String = row.get(7)?;
                let payload: String = row.get(8)?;
                let tags: String = row.get(9)?;
                let entities: String = row.get(10)?;
                Ok(Event {
                    event_id: row.get(0)?,
                    scope: Scope {
                        tenant_id: row.get(1)?,
                        user_id: row.get(2)?,
                        agent_id: row.get(3)?,
                        session_id: row.get(4)?,
                        run_id: row.get(5)?,
                    },
                    ts: from_millis(row.get(6)?),
                    kind: parse_enum(&kind, event_kind_from_str)?,
                    payload: decode_json_row(&payload)?,
                    tags: decode_json_row(&tags)?,
                    entities: decode_json_row(&entities)?,
                })
            })?;

            let mut events = Vec::new();
            for event in rows {
                events.push(event?);
            }
            Ok(events)
        })
    }

    fn get_working_state(&self, scope: &Scope) -> StoreResult<Option<WorkingState>> {
        self.with_connection(|conn| {
            let mut stmt = conn.prepare(
                "SELECT state_json FROM wm_state
                 WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?",
            )?;
            let result = stmt.query_row(
                params_from_iter(scope_params(scope)),
                |row| row.get::<_, String>(0),
            );
            match result {
                Ok(payload) => Ok(Some(decode_json(&payload)?)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(err) => Err(err.into()),
            }
        })
    }

    fn patch_working_state(
        &self,
        scope: &Scope,
        patch: WorkingStatePatch,
    ) -> StoreResult<WorkingState> {
        let current = self.get_working_state(scope)?.unwrap_or_default();
        let mut next = current.clone();

        let mut touched = false;
        if let Some(goal) = patch.goal {
            next.goal = goal;
            touched = true;
        }
        if let Some(plan) = patch.plan {
            next.plan = plan;
            touched = true;
        }
        if let Some(slots) = patch.slots {
            next.slots = slots;
            touched = true;
        }
        if let Some(constraints) = patch.constraints {
            next.constraints = constraints;
            touched = true;
        }
        if let Some(tool_evidence) = patch.tool_evidence {
            next.tool_evidence = tool_evidence;
            touched = true;
        }
        if let Some(decisions) = patch.decisions {
            next.decisions = decisions;
            touched = true;
        }
        if let Some(risks) = patch.risks {
            next.risks = risks;
            touched = true;
        }
        if let Some(state_version) = patch.state_version {
            next.state_version = state_version;
        } else if touched {
            next.state_version = next.state_version.saturating_add(1);
        }

        self.with_connection(|conn| {
            conn.execute(
                "
                INSERT INTO wm_state (
                    tenant_id, user_id, agent_id, session_id, run_id, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, user_id, agent_id, session_id, run_id)
                DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(scope.session_id.clone()),
                    SqlValue::Text(scope.run_id.clone()),
                    SqlValue::Text(encode_json(&next)?),
                    SqlValue::Integer(to_millis(Utc::now())),
                ]),
            )?;
            Ok(next)
        })
    }

    fn get_stm(&self, scope: &Scope) -> StoreResult<Option<StmState>> {
        self.with_connection(|conn| {
            let mut stmt = conn.prepare(
                "SELECT rolling_summary, key_quotes FROM stm_state
                 WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ?",
            )?;
            let result = stmt.query_row(
                params_from_iter(scope_params_session(scope)),
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            );

            match result {
                Ok((rolling_summary, key_quotes)) => Ok(Some(StmState {
                    rolling_summary,
                    key_quotes: decode_json(&key_quotes)?,
                })),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(err) => Err(err.into()),
            }
        })
    }

    fn update_stm(&self, scope: &Scope, stm: StmState) -> StoreResult<()> {
        self.with_connection(|conn| {
            conn.execute(
                "
                INSERT INTO stm_state (
                    tenant_id, user_id, agent_id, session_id, rolling_summary, key_quotes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, user_id, agent_id, session_id)
                DO UPDATE SET rolling_summary = excluded.rolling_summary,
                              key_quotes = excluded.key_quotes,
                              updated_at = excluded.updated_at
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(scope.session_id.clone()),
                    SqlValue::Text(stm.rolling_summary),
                    SqlValue::Text(encode_json(&stm.key_quotes)?),
                    SqlValue::Integer(to_millis(Utc::now())),
                ]),
            )?;
            Ok(())
        })
    }

    fn list_facts(&self, scope: &Scope, filter: FactFilter) -> StoreResult<Vec<Fact>> {
        self.with_connection(|conn| {
            let mut sql = String::from(
                "SELECT fact_id, fact_key, value_json, status, valid_from, valid_to,
                        confidence, sources, scope_level, notes
                 FROM facts WHERE tenant_id = ? AND user_id = ? AND agent_id = ?",
            );
            let mut params = scope_params_ltm(scope);

            if let Some(statuses) = &filter.status {
                if !statuses.is_empty() {
                    sql.push_str(" AND status IN (");
                    for (idx, status) in statuses.iter().enumerate() {
                        if idx > 0 {
                            sql.push_str(", ");
                        }
                        sql.push_str("?");
                        params.push(SqlValue::Text(fact_status_to_str(status).to_string()));
                    }
                    sql.push(')');
                }
            }

            if let Some(at) = filter.valid_at {
                sql.push_str(" AND (valid_from IS NULL OR valid_from <= ?)");
                params.push(SqlValue::Integer(to_millis(at)));
                sql.push_str(" AND (valid_to IS NULL OR valid_to >= ?)");
                params.push(SqlValue::Integer(to_millis(at)));
            }

            sql.push_str(" ORDER BY fact_key ASC, fact_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ?");
                params.push(SqlValue::Integer(limit as i64));
            }

            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(params), |row| {
                let value_json: String = row.get(2)?;
                let status: String = row.get(3)?;
                let sources: String = row.get(7)?;
                let scope_level: String = row.get(8)?;
                Ok(Fact {
                    fact_id: row.get(0)?,
                    fact_key: row.get(1)?,
                    value: decode_json_row(&value_json)?,
                    status: parse_enum(&status, fact_status_from_str)?,
                    validity: memweft_types::Validity {
                        valid_from: row.get::<_, Option<i64>>(4)?.map(from_millis),
                        valid_to: row.get::<_, Option<i64>>(5)?.map(from_millis),
                    },
                    confidence: row.get(6)?,
                    sources: decode_json_row(&sources)?,
                    scope_level: parse_enum(&scope_level, scope_level_from_str)?,
                    notes: row.get(9)?,
                })
            })?;

            let mut facts = Vec::new();
            for fact in rows {
                facts.push(fact?);
            }
            Ok(facts)
        })
    }

    fn upsert_fact(&self, scope: &Scope, fact: Fact) -> StoreResult<()> {
        self.with_connection(|conn| {
            conn.execute(
                "
                INSERT INTO facts (
                    tenant_id, user_id, agent_id, fact_id, fact_key, value_json, status,
                    valid_from, valid_to, confidence, sources, scope_level, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, user_id, agent_id, fact_id)
                DO UPDATE SET fact_key = excluded.fact_key,
                              value_json = excluded.value_json,
                              status = excluded.status,
                              valid_from = excluded.valid_from,
                              valid_to = excluded.valid_to,
                              confidence = excluded.confidence,
                              sources = excluded.sources,
                              scope_level = excluded.scope_level,
                              notes = excluded.notes
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(fact.fact_id),
                    SqlValue::Text(fact.fact_key),
                    SqlValue::Text(encode_json(&fact.value)?),
                    SqlValue::Text(fact_status_to_str(&fact.status).to_string()),
                    option_ts_to_value(fact.validity.valid_from),
                    option_ts_to_value(fact.validity.valid_to),
                    SqlValue::Real(fact.confidence),
                    SqlValue::Text(encode_json(&fact.sources)?),
                    SqlValue::Text(scope_level_to_str(&fact.scope_level).to_string()),
                    SqlValue::Text(fact.notes),
                ]),
            )?;
            Ok(())
        })
    }

    fn list_episodes(&self, scope: &Scope, filter: EpisodeFilter) -> StoreResult<Vec<Episode>> {
        self.with_connection(|conn| {
            let mut sql = String::from(
                "SELECT episode_id, start_ts, end_ts, summary, highlights, tags, entities,
                        sources, compression_level, recency_score
                 FROM episodes WHERE tenant_id = ? AND user_id = ? AND agent_id = ?",
            );
            let mut params = scope_params_ltm(scope);
            let mut filter_tags_in_memory = false;
            let mut filter_entities_in_memory = false;

            if let Some(range) = &filter.time_range {
                if let Some(start) = range.start {
                    sql.push_str(" AND start_ts >= ?");
                    params.push(SqlValue::Integer(to_millis(start)));
                }
                if let Some(end) = range.end {
                    sql.push_str(" AND COALESCE(end_ts, start_ts) <= ?");
                    params.push(SqlValue::Integer(to_millis(end)));
                }
            }

            if !filter.tags.is_empty() {
                if episode_tags_present(conn, scope)? {
                    let placeholders = sql_placeholders(filter.tags.len());
                    sql.push_str(
                        " AND EXISTS (
                            SELECT 1 FROM episode_tags t
                            WHERE t.tenant_id = episodes.tenant_id
                              AND t.user_id = episodes.user_id
                              AND t.agent_id = episodes.agent_id
                              AND t.episode_id = episodes.episode_id
                              AND t.tag IN (",
                    );
                    sql.push_str(&placeholders);
                    sql.push_str("))");
                    for tag in &filter.tags {
                        params.push(SqlValue::Text(tag.clone()));
                    }
                } else {
                    filter_tags_in_memory = true;
                }
            }

            if !filter.entities.is_empty() {
                if episode_entities_present(conn, scope)? {
                    let placeholders = sql_placeholders(filter.entities.len());
                    sql.push_str(
                        " AND EXISTS (
                            SELECT 1 FROM episode_entities e
                            WHERE e.tenant_id = episodes.tenant_id
                              AND e.user_id = episodes.user_id
                              AND e.agent_id = episodes.agent_id
                              AND e.episode_id = episodes.episode_id
                              AND e.entity IN (",
                    );
                    sql.push_str(&placeholders);
                    sql.push_str("))");
                    for entity in &filter.entities {
                        params.push(SqlValue::Text(entity.clone()));
                    }
                } else {
                    filter_entities_in_memory = true;
                }
            }

            sql.push_str(" ORDER BY start_ts ASC, episode_id ASC");
            let limit_in_sql = if filter_tags_in_memory || filter_entities_in_memory {
                None
            } else {
                filter.limit
            };
            if let Some(limit) = limit_in_sql {
                sql.push_str(" LIMIT ?");
                params.push(SqlValue::Integer(limit as i64));
            }

            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(params), |row| {
                let highlights: String = row.get(4)?;
                let tags: String = row.get(5)?;
                let entities: String = row.get(6)?;
                let sources: String = row.get(7)?;
                let compression_level: String = row.get(8)?;
                Ok(Episode {
                    episode_id: row.get(0)?,
                    time_range: memweft_types::TimeRange {
                        start: from_millis(row.get(1)?),
                        end: row.get::<_, Option<i64>>(2)?.map(from_millis),
                    },
                    summary: row.get(3)?,
                    highlights: decode_json_row(&highlights)?,
                    tags: decode_json_row(&tags)?,
                    entities: decode_json_row(&entities)?,
                    sources: decode_json_row(&sources)?,
                    compression_level: parse_enum(&compression_level, compression_level_from_str)?,
                    recency_score: row.get(9)?,
                })
            })?;

            let mut episodes = Vec::new();
            for episode in rows {
                episodes.push(episode?);
            }

            if !filter_tags_in_memory && !filter_entities_in_memory {
                return Ok(episodes);
            }

            let mut filtered = episodes
                .into_iter()
                .filter(|episode| {
                    let tags_ok = if filter_tags_in_memory {
                        episode.tags.iter().any(|tag| filter.tags.contains(tag))
                    } else {
                        true
                    };
                    let entities_ok = if filter_entities_in_memory {
                        episode
                            .entities
                            .iter()
                            .any(|entity| filter.entities.contains(entity))
                    } else {
                        true
                    };
                    tags_ok && entities_ok
                })
                .collect::<Vec<_>>();

            if limit_in_sql.is_none() {
                if let Some(limit) = filter.limit {
                    if filtered.len() > limit {
                        filtered.truncate(limit);
                    }
                }
            }

            Ok(filtered)
        })
    }

    fn append_episode(&self, scope: &Scope, episode: Episode) -> StoreResult<()> {
        self.with_connection(|conn| {
            let tx = conn.transaction()?;
            tx.execute(
                "
                INSERT INTO episodes (
                    tenant_id, user_id, agent_id, episode_id, start_ts, end_ts, summary,
                    highlights, tags, entities, sources, compression_level, recency_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(episode.episode_id.clone()),
                    SqlValue::Integer(to_millis(episode.time_range.start)),
                    option_ts_to_value(episode.time_range.end),
                    SqlValue::Text(episode.summary),
                    SqlValue::Text(encode_json(&episode.highlights)?),
                    SqlValue::Text(encode_json(&episode.tags)?),
                    SqlValue::Text(encode_json(&episode.entities)?),
                    SqlValue::Text(encode_json(&episode.sources)?),
                    SqlValue::Text(compression_level_to_str(&episode.compression_level).to_string()),
                    option_f64_to_value(episode.recency_score),
                ]),
            )?;
            insert_episode_tags(
                &tx,
                scope,
                &episode.episode_id,
                &episode.tags,
                &episode.entities,
            )?;
            tx.commit()?;
            Ok(())
        })
    }

    fn list_procedures(
        &self,
        scope: &Scope,
        task_type: &str,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Procedure>> {
        self.with_connection(|conn| {
            let mut sql = String::from(
                "SELECT procedure_id, task_type, content_json, priority, sources, applicability
                 FROM procedures WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND task_type = ?",
            );
            let mut params = scope_params_ltm(scope);
            params.push(SqlValue::Text(task_type.to_string()));

            sql.push_str(" ORDER BY priority DESC, procedure_id ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ?");
                params.push(SqlValue::Integer(limit as i64));
            }

            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(params), |row| {
                let content: String = row.get(2)?;
                let sources: String = row.get(4)?;
                let applicability: String = row.get(5)?;
                Ok(Procedure {
                    procedure_id: row.get(0)?,
                    task_type: row.get(1)?,
                    content: decode_json_row(&content)?,
                    priority: row.get(3)?,
                    sources: decode_json_row(&sources)?,
                    applicability: decode_json_row(&applicability)?,
                })
            })?;

            let mut procedures = Vec::new();
            for procedure in rows {
                procedures.push(procedure?);
            }
            Ok(procedures)
        })
    }

    fn upsert_procedure(&self, scope: &Scope, procedure: Procedure) -> StoreResult<()> {
        self.with_connection(|conn| {
            conn.execute(
                "
                INSERT INTO procedures (
                    tenant_id, user_id, agent_id, procedure_id, task_type, content_json,
                    priority, sources, applicability
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, user_id, agent_id, procedure_id)
                DO UPDATE SET task_type = excluded.task_type,
                              content_json = excluded.content_json,
                              priority = excluded.priority,
                              sources = excluded.sources,
                              applicability = excluded.applicability
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(procedure.procedure_id),
                    SqlValue::Text(procedure.task_type),
                    SqlValue::Text(encode_json(&procedure.content)?),
                    SqlValue::Integer(procedure.priority as i64),
                    SqlValue::Text(encode_json(&procedure.sources)?),
                    SqlValue::Text(encode_json(&procedure.applicability)?),
                ]),
            )?;
            Ok(())
        })
    }

    fn list_insights(&self, scope: &Scope, filter: InsightFilter) -> StoreResult<Vec<InsightItem>> {
        self.with_connection(|conn| {
            let mut sql = String::from(
                "SELECT insight_id, kind, statement, trigger, confidence, validation_state,
                        tests_suggested, expires_at, sources
                 FROM insights
                 WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?",
            );
            let mut params = scope_params(scope);

            if let Some(states) = &filter.validation_state {
                if !states.is_empty() {
                    sql.push_str(" AND validation_state IN (");
                    for (idx, state) in states.iter().enumerate() {
                        if idx > 0 {
                            sql.push_str(", ");
                        }
                        sql.push_str("?");
                        params.push(SqlValue::Text(validation_state_to_str(state).to_string()));
                    }
                    sql.push(')');
                }
            }

            sql.push_str(" ORDER BY validation_state DESC, confidence DESC, insight_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ?");
                params.push(SqlValue::Integer(limit as i64));
            }

            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(params), |row| {
                let kind: String = row.get(1)?;
                let trigger: String = row.get(3)?;
                let validation_state: String = row.get(5)?;
                let tests: String = row.get(6)?;
                let sources: String = row.get(8)?;
                Ok(InsightItem {
                    id: row.get(0)?,
                    kind: parse_enum(&kind, insight_type_from_str)?,
                    statement: row.get(2)?,
                    trigger: parse_enum(&trigger, insight_trigger_from_str)?,
                    confidence: row.get(4)?,
                    validation_state: parse_enum(&validation_state, validation_state_from_str)?,
                    tests_suggested: decode_json_row(&tests)?,
                    expires_at: row.get(7)?,
                    sources: decode_json_row(&sources)?,
                })
            })?;

            let mut insights = Vec::new();
            for insight in rows {
                insights.push(insight?);
            }
            Ok(insights)
        })
    }

    fn append_insight(&self, scope: &Scope, insight: InsightItem) -> StoreResult<()> {
        self.with_connection(|conn| {
            conn.execute(
                "
                INSERT INTO insights (
                    tenant_id, user_id, agent_id, session_id, run_id, insight_id,
                    kind, statement, trigger, confidence, validation_state,
                    tests_suggested, expires_at, sources
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(scope.session_id.clone()),
                    SqlValue::Text(scope.run_id.clone()),
                    SqlValue::Text(insight.id),
                    SqlValue::Text(insight_type_to_str(&insight.kind).to_string()),
                    SqlValue::Text(insight.statement),
                    SqlValue::Text(insight_trigger_to_str(&insight.trigger).to_string()),
                    SqlValue::Real(insight.confidence),
                    SqlValue::Text(validation_state_to_str(&insight.validation_state).to_string()),
                    SqlValue::Text(encode_json(&insight.tests_suggested)?),
                    SqlValue::Text(insight.expires_at),
                    SqlValue::Text(encode_json(&insight.sources)?),
                ]),
            )?;
            Ok(())
        })
    }

    fn write_context_build(&self, scope: &Scope, packet: MemoryPacket) -> StoreResult<()> {
        self.with_connection(|conn| {
            let generated = to_millis(packet.meta.generated_at);
            conn.execute(
                "
                INSERT INTO context_builds (
                    tenant_id, user_id, agent_id, session_id, run_id, ts, packet_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ",
                params_from_iter(vec![
                    SqlValue::Text(scope.tenant_id.clone()),
                    SqlValue::Text(scope.user_id.clone()),
                    SqlValue::Text(scope.agent_id.clone()),
                    SqlValue::Text(scope.session_id.clone()),
                    SqlValue::Text(scope.run_id.clone()),
                    SqlValue::Integer(generated),
                    SqlValue::Text(encode_json(&packet)?),
                ]),
            )?;
            Ok(())
        })
    }

    fn list_context_builds(
        &self,
        scope: &Scope,
        limit: Option<usize>,
    ) -> StoreResult<Vec<MemoryPacket>> {
        self.with_connection(|conn| {
            let mut sql = String::from(
                "SELECT packet_json FROM context_builds
                 WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?
                 ORDER BY ts ASC",
            );
            let mut params = scope_params(scope);
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ?");
                params.push(SqlValue::Integer(limit as i64));
            }

            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(params_from_iter(params), |row| row.get::<_, String>(0))?;
            let mut packets = Vec::new();
            for row in rows {
                packets.push(decode_json(&row?)?);
            }
            Ok(packets)
        })
    }
}

fn encode_json<T: Serialize>(value: &T) -> StoreResult<String> {
    Ok(serde_json::to_string(value)?)
}

fn decode_json<T: DeserializeOwned>(value: &str) -> StoreResult<T> {
    Ok(serde_json::from_str(value)?)
}

fn decode_json_row<T: DeserializeOwned>(value: &str) -> rusqlite::Result<T> {
    serde_json::from_str(value)
        .map_err(|err| rusqlite::Error::FromSqlConversionFailure(0, Type::Text, Box::new(err)))
}

fn to_millis(ts: DateTime<Utc>) -> i64 {
    ts.timestamp_millis()
}

fn from_millis(millis: i64) -> DateTime<Utc> {
    Utc.timestamp_millis_opt(millis)
        .single()
        .unwrap_or_else(|| Utc.timestamp_millis_opt(0).single().unwrap())
}

fn option_ts_to_value(value: Option<DateTime<Utc>>) -> SqlValue {
    match value {
        Some(ts) => SqlValue::Integer(to_millis(ts)),
        None => SqlValue::Null,
    }
}

fn option_f64_to_value(value: Option<f64>) -> SqlValue {
    match value {
        Some(number) => SqlValue::Real(number),
        None => SqlValue::Null,
    }
}

fn scope_params(scope: &Scope) -> Vec<SqlValue> {
    vec![
        SqlValue::Text(scope.tenant_id.clone()),
        SqlValue::Text(scope.user_id.clone()),
        SqlValue::Text(scope.agent_id.clone()),
        SqlValue::Text(scope.session_id.clone()),
        SqlValue::Text(scope.run_id.clone()),
    ]
}

fn scope_params_session(scope: &Scope) -> Vec<SqlValue> {
    vec![
        SqlValue::Text(scope.tenant_id.clone()),
        SqlValue::Text(scope.user_id.clone()),
        SqlValue::Text(scope.agent_id.clone()),
        SqlValue::Text(scope.session_id.clone()),
    ]
}

fn scope_params_ltm(scope: &Scope) -> Vec<SqlValue> {
    vec![
        SqlValue::Text(scope.tenant_id.clone()),
        SqlValue::Text(scope.user_id.clone()),
        SqlValue::Text(scope.agent_id.clone()),
    ]
}

fn unique_values(values: &[String]) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for value in values {
        if seen.insert(value.clone()) {
            out.push(value.clone());
        }
    }
    out
}

fn sql_placeholders(count: usize) -> String {
    let mut out = String::new();
    for idx in 0..count {
        if idx > 0 {
            out.push_str(", ");
        }
        out.push('?');
    }
    out
}

fn insert_event_tags(
    conn: &Connection,
    scope: &Scope,
    event_id: &str,
    tags: &[String],
    entities: &[String],
) -> StoreResult<()> {
    if tags.is_empty() && entities.is_empty() {
        return Ok(());
    }
    let tags = unique_values(tags);
    let entities = unique_values(entities);

    if !tags.is_empty() {
        let mut stmt = conn.prepare(
            "
            INSERT OR IGNORE INTO event_tags (
                tenant_id, user_id, agent_id, session_id, run_id, event_id, tag
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ",
        )?;
        for tag in tags {
            stmt.execute(params_from_iter(vec![
                SqlValue::Text(scope.tenant_id.clone()),
                SqlValue::Text(scope.user_id.clone()),
                SqlValue::Text(scope.agent_id.clone()),
                SqlValue::Text(scope.session_id.clone()),
                SqlValue::Text(scope.run_id.clone()),
                SqlValue::Text(event_id.to_string()),
                SqlValue::Text(tag),
            ]))?;
        }
    }

    if !entities.is_empty() {
        let mut stmt = conn.prepare(
            "
            INSERT OR IGNORE INTO event_entities (
                tenant_id, user_id, agent_id, session_id, run_id, event_id, entity
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ",
        )?;
        for entity in entities {
            stmt.execute(params_from_iter(vec![
                SqlValue::Text(scope.tenant_id.clone()),
                SqlValue::Text(scope.user_id.clone()),
                SqlValue::Text(scope.agent_id.clone()),
                SqlValue::Text(scope.session_id.clone()),
                SqlValue::Text(scope.run_id.clone()),
                SqlValue::Text(event_id.to_string()),
                SqlValue::Text(entity),
            ]))?;
        }
    }

    Ok(())
}

fn insert_episode_tags(
    conn: &Connection,
    scope: &Scope,
    episode_id: &str,
    tags: &[String],
    entities: &[String],
) -> StoreResult<()> {
    if tags.is_empty() && entities.is_empty() {
        return Ok(());
    }
    let tags = unique_values(tags);
    let entities = unique_values(entities);

    if !tags.is_empty() {
        let mut stmt = conn.prepare(
            "
            INSERT OR IGNORE INTO episode_tags (
                tenant_id, user_id, agent_id, episode_id, tag
            ) VALUES (?, ?, ?, ?, ?)
            ",
        )?;
        for tag in tags {
            stmt.execute(params_from_iter(vec![
                SqlValue::Text(scope.tenant_id.clone()),
                SqlValue::Text(scope.user_id.clone()),
                SqlValue::Text(scope.agent_id.clone()),
                SqlValue::Text(episode_id.to_string()),
                SqlValue::Text(tag),
            ]))?;
        }
    }

    if !entities.is_empty() {
        let mut stmt = conn.prepare(
            "
            INSERT OR IGNORE INTO episode_entities (
                tenant_id, user_id, agent_id, episode_id, entity
            ) VALUES (?, ?, ?, ?, ?)
            ",
        )?;
        for entity in entities {
            stmt.execute(params_from_iter(vec![
                SqlValue::Text(scope.tenant_id.clone()),
                SqlValue::Text(scope.user_id.clone()),
                SqlValue::Text(scope.agent_id.clone()),
                SqlValue::Text(episode_id.to_string()),
                SqlValue::Text(entity),
            ]))?;
        }
    }

    Ok(())
}

fn episode_tags_present(conn: &Connection, scope: &Scope) -> StoreResult<bool> {
    let mut stmt = conn.prepare(
        "SELECT 1 FROM episode_tags WHERE tenant_id = ? AND user_id = ? AND agent_id = ? LIMIT 1",
    )?;
    match stmt.query_row(params_from_iter(scope_params_ltm(scope)), |_| Ok(())) {
        Ok(_) => Ok(true),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(false),
        Err(err) => Err(err.into()),
    }
}

fn episode_entities_present(conn: &Connection, scope: &Scope) -> StoreResult<bool> {
    let mut stmt = conn.prepare(
        "SELECT 1 FROM episode_entities WHERE tenant_id = ? AND user_id = ? AND agent_id = ? LIMIT 1",
    )?;
    match stmt.query_row(params_from_iter(scope_params_ltm(scope)), |_| Ok(())) {
        Ok(_) => Ok(true),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(false),
        Err(err) => Err(err.into()),
    }
}

fn event_kind_to_str(kind: &EventKind) -> &'static str {
    match kind {
        EventKind::Message => "message",
        EventKind::ToolResult => "tool_result",
        EventKind::StatePatch => "state_patch",
        EventKind::System => "system",
    }
}

fn event_kind_from_str(value: &str) -> Option<EventKind> {
    match value {
        "message" => Some(EventKind::Message),
        "tool_result" => Some(EventKind::ToolResult),
        "state_patch" => Some(EventKind::StatePatch),
        "system" => Some(EventKind::System),
        _ => None,
    }
}

fn fact_status_to_str(status: &FactStatus) -> &'static str {
    match status {
        FactStatus::Active => "active",
        FactStatus::Disputed => "disputed",
        FactStatus::Deprecated => "deprecated",
    }
}

fn fact_status_from_str(value: &str) -> Option<FactStatus> {
    match value {
        "active" => Some(FactStatus::Active),
        "disputed" => Some(FactStatus::Disputed),
        "deprecated" => Some(FactStatus::Deprecated),
        _ => None,
    }
}

fn scope_level_to_str(level: &ScopeLevel) -> &'static str {
    match level {
        ScopeLevel::User => "user",
        ScopeLevel::Agent => "agent",
        ScopeLevel::Tenant => "tenant",
    }
}

fn scope_level_from_str(value: &str) -> Option<ScopeLevel> {
    match value {
        "user" => Some(ScopeLevel::User),
        "agent" => Some(ScopeLevel::Agent),
        "tenant" => Some(ScopeLevel::Tenant),
        _ => None,
    }
}

fn compression_level_to_str(level: &CompressionLevel) -> &'static str {
    match level {
        CompressionLevel::Raw => "raw",
        CompressionLevel::PhaseSummary => "phase_summary",
        CompressionLevel::Milestone => "milestone",
        CompressionLevel::Theme => "theme",
    }
}

fn compression_level_from_str(value: &str) -> Option<CompressionLevel> {
    match value {
        "raw" => Some(CompressionLevel::Raw),
        "phase_summary" => Some(CompressionLevel::PhaseSummary),
        "milestone" => Some(CompressionLevel::Milestone),
        "theme" => Some(CompressionLevel::Theme),
        _ => None,
    }
}

fn insight_type_to_str(kind: &InsightType) -> &'static str {
    match kind {
        InsightType::Hypothesis => "hypothesis",
        InsightType::Strategy => "strategy",
        InsightType::Pattern => "pattern",
    }
}

fn insight_type_from_str(value: &str) -> Option<InsightType> {
    match value {
        "hypothesis" => Some(InsightType::Hypothesis),
        "strategy" => Some(InsightType::Strategy),
        "pattern" => Some(InsightType::Pattern),
        _ => None,
    }
}

fn insight_trigger_to_str(trigger: &InsightTrigger) -> &'static str {
    match trigger {
        InsightTrigger::Conflict => "conflict",
        InsightTrigger::Failure => "failure",
        InsightTrigger::Synthesis => "synthesis",
        InsightTrigger::Analogy => "analogy",
    }
}

fn insight_trigger_from_str(value: &str) -> Option<InsightTrigger> {
    match value {
        "conflict" => Some(InsightTrigger::Conflict),
        "failure" => Some(InsightTrigger::Failure),
        "synthesis" => Some(InsightTrigger::Synthesis),
        "analogy" => Some(InsightTrigger::Analogy),
        _ => None,
    }
}

fn validation_state_to_str(state: &ValidationState) -> &'static str {
    match state {
        ValidationState::Unvalidated => "unvalidated",
        ValidationState::Testing => "testing",
        ValidationState::Validated => "validated",
        ValidationState::Rejected => "rejected",
    }
}

fn validation_state_from_str(value: &str) -> Option<ValidationState> {
    match value {
        "unvalidated" => Some(ValidationState::Unvalidated),
        "testing" => Some(ValidationState::Testing),
        "validated" => Some(ValidationState::Validated),
        "rejected" => Some(ValidationState::Rejected),
        _ => None,
    }
}

fn parse_enum<T>(value: &str, parser: fn(&str) -> Option<T>) -> rusqlite::Result<T> {
    parser(value).ok_or_else(|| {
        rusqlite::Error::FromSqlConversionFailure(
            0,
            Type::Text,
            Box::new(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "invalid enum value",
            )),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Store, TimeRangeFilter};
    use memweft_types::{
        Budget, FactStatus, InsightItem, InsightTrigger, InsightType, JsonMap, Meta, Purpose, Scope,
        ScopeLevel, ShortTerm, Validity, ValidationState,
    };
    use serde_json::json;

    fn sample_scope() -> Scope {
        Scope {
            tenant_id: "default".to_string(),
            user_id: "user1".to_string(),
            agent_id: "agent1".to_string(),
            session_id: "session1".to_string(),
            run_id: "run1".to_string(),
        }
    }

    fn sample_packet(scope: Scope) -> MemoryPacket {
        MemoryPacket {
            meta: Meta {
                schema_version: "v1".to_string(),
                scope,
                generated_at: Utc::now(),
                purpose: Purpose::Planner,
                task_type: "generic".to_string(),
                cues: JsonMap::new(),
                budget: Budget {
                    max_tokens: 512,
                    per_section: JsonMap::new(),
                },
                policy_id: "default".to_string(),
            },
            short_term: ShortTerm::default(),
            long_term: memweft_types::LongTerm::default(),
            insight: memweft_types::Insight::default(),
            citations: Vec::new(),
            budget_report: memweft_types::BudgetReport::default(),
            explain: JsonMap::new(),
        }
    }

    #[test]
    fn sqlite_store_roundtrip() {
        let store = SqliteStore::new_in_memory().unwrap();
        let scope = sample_scope();

        store
            .append_event(Event {
                event_id: "e1".to_string(),
                scope: scope.clone(),
                ts: Utc::now(),
                kind: EventKind::Message,
                payload: json!({ "role": "user", "content": "hi" }),
                tags: vec!["alpha".to_string()],
                entities: vec!["entity1".to_string()],
            })
            .unwrap();

        let events = store
            .list_events(&scope, TimeRangeFilter::default(), None)
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_id, "e1");

        let state = store
            .patch_working_state(
                &scope,
                WorkingStatePatch {
                    goal: Some("ship".to_string()),
                    ..WorkingStatePatch::default()
                },
            )
            .unwrap();
        assert_eq!(state.goal, "ship");

        store
            .update_stm(
                &scope,
                StmState {
                    rolling_summary: "summary".to_string(),
                    key_quotes: vec![],
                },
            )
            .unwrap();
        assert!(store.get_stm(&scope).unwrap().is_some());

        store
            .upsert_fact(
                &scope,
                Fact {
                    fact_id: "f1".to_string(),
                    fact_key: "pref.color".to_string(),
                    value: json!("blue"),
                    status: FactStatus::Active,
                    validity: Validity::default(),
                    confidence: 0.9,
                    sources: vec!["e1".to_string()],
                    scope_level: ScopeLevel::User,
                    notes: String::new(),
                },
            )
            .unwrap();
        store
            .upsert_fact(
                &scope,
                Fact {
                    fact_id: "f2".to_string(),
                    fact_key: "old.pref".to_string(),
                    value: json!("red"),
                    status: FactStatus::Deprecated,
                    validity: Validity::default(),
                    confidence: 0.1,
                    sources: vec![],
                    scope_level: ScopeLevel::User,
                    notes: String::new(),
                },
            )
            .unwrap();

        let facts = store
            .list_facts(
                &scope,
                FactFilter {
                    status: Some(vec![FactStatus::Active]),
                    ..FactFilter::default()
                },
            )
            .unwrap();
        assert_eq!(facts.len(), 1);

        store
            .append_episode(
                &scope,
                Episode {
                    episode_id: "ep1".to_string(),
                    time_range: memweft_types::TimeRange {
                        start: Utc::now(),
                        end: None,
                    },
                    summary: "did a thing".to_string(),
                    highlights: vec!["h1".to_string()],
                    tags: vec!["alpha".to_string()],
                    entities: vec!["entity1".to_string()],
                    sources: vec!["e1".to_string()],
                    compression_level: CompressionLevel::Raw,
                    recency_score: None,
                },
            )
            .unwrap();
        store
            .append_episode(
                &scope,
                Episode {
                    episode_id: "ep2".to_string(),
                    time_range: memweft_types::TimeRange {
                        start: Utc::now(),
                        end: None,
                    },
                    summary: "did another".to_string(),
                    highlights: vec![],
                    tags: vec!["beta".to_string()],
                    entities: vec![],
                    sources: vec![],
                    compression_level: CompressionLevel::Raw,
                    recency_score: None,
                },
            )
            .unwrap();

        let episodes = store
            .list_episodes(
                &scope,
                EpisodeFilter {
                    tags: vec!["alpha".to_string()],
                    ..EpisodeFilter::default()
                },
            )
            .unwrap();
        assert_eq!(episodes.len(), 1);

        store
            .upsert_procedure(
                &scope,
                Procedure {
                    procedure_id: "p1".to_string(),
                    task_type: "generic".to_string(),
                    content: json!({"steps": ["a", "b"]}),
                    priority: 10,
                    sources: vec![],
                    applicability: JsonMap::new(),
                },
            )
            .unwrap();

        let procedures = store
            .list_procedures(&scope, "generic", Some(5))
            .unwrap();
        assert_eq!(procedures.len(), 1);

        store
            .append_insight(
                &scope,
                InsightItem {
                    id: "i1".to_string(),
                    kind: InsightType::Hypothesis,
                    statement: "maybe".to_string(),
                    trigger: InsightTrigger::Synthesis,
                    confidence: 0.3,
                    validation_state: ValidationState::Validated,
                    tests_suggested: vec![],
                    expires_at: "run_end".to_string(),
                    sources: vec![],
                },
            )
            .unwrap();

        let insights = store
            .list_insights(
                &scope,
                InsightFilter {
                    validation_state: Some(vec![ValidationState::Validated]),
                    ..InsightFilter::default()
                },
            )
            .unwrap();
        assert_eq!(insights.len(), 1);

        store
            .write_context_build(&scope, sample_packet(scope.clone()))
            .unwrap();
        let builds = store.list_context_builds(&scope, None).unwrap();
        assert_eq!(builds.len(), 1);
    }
}

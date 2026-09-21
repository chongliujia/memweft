use chrono::{DateTime, TimeZone, Utc};
use memweft_types::{
    CompressionLevel, Episode, Fact, FactStatus, InsightItem, InsightTrigger, InsightType,
    MemoryPacket, Procedure, Scope, ScopeLevel, ValidationState, WorkingState,
};
use mysql::prelude::Queryable;
use mysql::{from_row, Opts, OptsBuilder, Params, Pool, PooledConn, Value as MyValue};
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::collections::HashSet;

use crate::{
    EpisodeFilter, Event, EventKind, FactFilter, InsightFilter, StmState, Store, StoreError,
    StoreResult, TimeRangeFilter, WorkingStatePatch,
};

const SCHEMA_VERSION: i64 = 1;

pub struct MySqlStore {
    pool: Pool,
}

impl std::fmt::Debug for MySqlStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MySqlStore").finish()
    }
}

impl MySqlStore {
    pub fn new(url: &str) -> StoreResult<Self> {
        let mut opts =
            mysql::Opts::from_url(url).map_err(|err| StoreError::InvalidInput(err.to_string()))?;
        let db_name = opts
            .get_db_name()
            .map(|value| value.to_string())
            .unwrap_or_else(|| "memweft".to_string());
        if opts.get_db_name().is_none() {
            opts = OptsBuilder::from_opts(opts)
                .db_name(Some(db_name.clone()))
                .into();
        }
        let pool = match Pool::new(opts.clone()) {
            Ok(pool) => pool,
            Err(err) if is_unknown_database(&err) => {
                ensure_mysql_database(&opts, &db_name)?;
                Pool::new(opts).map_err(map_mysql_err)?
            }
            Err(err) => return Err(map_mysql_err(err)),
        };
        let store = Self { pool };
        store.with_conn(|conn| ensure_schema(conn))?;
        Ok(store)
    }

    fn with_conn<F, T>(&self, f: F) -> StoreResult<T>
    where
        F: FnOnce(&mut PooledConn) -> StoreResult<T>,
    {
        let mut conn = self.pool.get_conn().map_err(map_mysql_err)?;
        f(&mut conn)
    }

    pub fn append_events_bulk(&self, events: &[Event]) -> StoreResult<()> {
        if events.is_empty() {
            return Ok(());
        }
        self.with_conn(|conn| {
            conn.exec_drop("START TRANSACTION", ())
                .map_err(map_mysql_err)?;
            let result = (|| {
                let mut params = Vec::with_capacity(events.len());
                for event in events {
                    params.push(Params::Positional(vec![
                        MyValue::from(event.event_id.clone()),
                        MyValue::from(event.scope.tenant_id.clone()),
                        MyValue::from(event.scope.user_id.clone()),
                        MyValue::from(event.scope.agent_id.clone()),
                        MyValue::from(event.scope.session_id.clone()),
                        MyValue::from(event.scope.run_id.clone()),
                        MyValue::from(to_millis(event.ts)),
                        MyValue::from(event_kind_to_str(&event.kind)),
                        MyValue::from(encode_json(&event.payload)?),
                        MyValue::from(encode_json(&event.tags)?),
                        MyValue::from(encode_json(&event.entities)?),
                    ]));
                }

                conn.exec_batch(
                    "INSERT INTO events (
                        event_id, tenant_id, user_id, agent_id, session_id, run_id,
                        ts, kind, payload, tags, entities
                     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
                .map_err(map_mysql_err)?;

                insert_event_tags_bulk(conn, events)?;
                Ok(())
            })();

            match result {
                Ok(()) => {
                    conn.exec_drop("COMMIT", ()).map_err(map_mysql_err)?;
                    Ok(())
                }
                Err(err) => {
                    let _ = conn.exec_drop("ROLLBACK", ());
                    Err(err)
                }
            }
        })
    }
}

impl Store for MySqlStore {
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
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO events (
                    event_id, tenant_id, user_id, agent_id, session_id, run_id,
                    ts, kind, payload, tags, entities
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id.clone(),
                    scope.tenant_id.clone(),
                    scope.user_id.clone(),
                    scope.agent_id.clone(),
                    scope.session_id.clone(),
                    scope.run_id.clone(),
                    to_millis(ts),
                    event_kind_to_str(&kind),
                    encode_json(&payload)?,
                    encode_json(&tags)?,
                    encode_json(&entities)?,
                ),
            )
            .map_err(map_mysql_err)?;
            insert_event_tags(conn, &scope, &event_id, &tags, &entities)?;
            Ok(())
        })
    }

    fn list_events(
        &self,
        scope: &Scope,
        range: TimeRangeFilter,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Event>> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT event_id, tenant_id, user_id, agent_id, session_id, run_id,
                        ts, kind, payload, tags, entities
                 FROM events WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?",
            );
            let mut params = scope_params(scope);

            if let Some(start) = range.start {
                sql.push_str(" AND ts >= ?");
                params.push(MyValue::from(to_millis(start)));
            }
            if let Some(end) = range.end {
                sql.push_str(" AND ts <= ?");
                params.push(MyValue::from(to_millis(end)));
            }
            sql.push_str(" ORDER BY ts ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ?");
                params.push(MyValue::from(limit as i64));
            }

            let rows: Vec<mysql::Row> =
                conn.exec(sql, Params::Positional(params))
                    .map_err(map_mysql_err)?;
            let mut events = Vec::with_capacity(rows.len());
            for row in rows {
                let (
                    event_id,
                    tenant_id,
                    user_id,
                    agent_id,
                    session_id,
                    run_id,
                    ts,
                    kind,
                    payload,
                    tags,
                    entities,
                ): (String, String, String, String, String, String, i64, String, String, String, String) =
                    from_row(row);
                events.push(Event {
                    event_id,
                    scope: Scope {
                        tenant_id,
                        user_id,
                        agent_id,
                        session_id,
                        run_id,
                    },
                    ts: from_millis(ts),
                    kind: parse_event_kind(&kind)?,
                    payload: decode_json(&payload)?,
                    tags: decode_json(&tags)?,
                    entities: decode_json(&entities)?,
                });
            }
            Ok(events)
        })
    }

    fn get_working_state(&self, scope: &Scope) -> StoreResult<Option<WorkingState>> {
        self.with_conn(|conn| {
            let row: Option<String> = conn
                .exec_first(
                    "SELECT state_json FROM wm_state
                     WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?",
                    (
                        scope.tenant_id.clone(),
                        scope.user_id.clone(),
                        scope.agent_id.clone(),
                        scope.session_id.clone(),
                        scope.run_id.clone(),
                    ),
                )
                .map_err(map_mysql_err)?;
            match row {
                Some(payload) => Ok(Some(decode_json(&payload)?)),
                None => Ok(None),
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

        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO wm_state (
                    tenant_id, user_id, agent_id, session_id, run_id, state_json, updated_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON DUPLICATE KEY UPDATE state_json = VALUES(state_json),
                                         updated_at = VALUES(updated_at)",
                (
                    scope.tenant_id.clone(),
                    scope.user_id.clone(),
                    scope.agent_id.clone(),
                    scope.session_id.clone(),
                    scope.run_id.clone(),
                    encode_json(&next)?,
                    to_millis(Utc::now()),
                ),
            )
            .map_err(map_mysql_err)?;
            Ok(next)
        })
    }

    fn get_stm(&self, scope: &Scope) -> StoreResult<Option<StmState>> {
        self.with_conn(|conn| {
            let row: Option<(String, String)> = conn
                .exec_first(
                    "SELECT rolling_summary, key_quotes FROM stm_state
                     WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ?",
                    (
                        scope.tenant_id.clone(),
                        scope.user_id.clone(),
                        scope.agent_id.clone(),
                        scope.session_id.clone(),
                    ),
                )
                .map_err(map_mysql_err)?;
            match row {
                Some((rolling_summary, key_quotes)) => Ok(Some(StmState {
                    rolling_summary,
                    key_quotes: decode_json(&key_quotes)?,
                })),
                None => Ok(None),
            }
        })
    }

    fn update_stm(&self, scope: &Scope, stm: StmState) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO stm_state (
                    tenant_id, user_id, agent_id, session_id, rolling_summary, key_quotes, updated_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON DUPLICATE KEY UPDATE rolling_summary = VALUES(rolling_summary),
                                         key_quotes = VALUES(key_quotes),
                                         updated_at = VALUES(updated_at)",
                (
                    scope.tenant_id.clone(),
                    scope.user_id.clone(),
                    scope.agent_id.clone(),
                    scope.session_id.clone(),
                    stm.rolling_summary,
                    encode_json(&stm.key_quotes)?,
                    to_millis(Utc::now()),
                ),
            )
            .map_err(map_mysql_err)?;
            Ok(())
        })
    }

    fn list_facts(&self, scope: &Scope, filter: FactFilter) -> StoreResult<Vec<Fact>> {
        self.with_conn(|conn| {
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
                        params.push(MyValue::from(fact_status_to_str(status)));
                    }
                    sql.push(')');
                }
            }

            if let Some(at) = filter.valid_at {
                sql.push_str(" AND (valid_from IS NULL OR valid_from <= ?)");
                params.push(MyValue::from(to_millis(at)));
                sql.push_str(" AND (valid_to IS NULL OR valid_to >= ?)");
                params.push(MyValue::from(to_millis(at)));
            }

            sql.push_str(" ORDER BY fact_key ASC, fact_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ?");
                params.push(MyValue::from(limit as i64));
            }

            let rows: Vec<mysql::Row> =
                conn.exec(sql, Params::Positional(params))
                    .map_err(map_mysql_err)?;
            let mut facts = Vec::with_capacity(rows.len());
            for row in rows {
                let (
                    fact_id,
                    fact_key,
                    value_json,
                    status,
                    valid_from,
                    valid_to,
                    confidence,
                    sources,
                    scope_level,
                    notes,
                ): (
                    String,
                    String,
                    String,
                    String,
                    Option<i64>,
                    Option<i64>,
                    f64,
                    String,
                    String,
                    String,
                ) = from_row(row);
                facts.push(Fact {
                    fact_id,
                    fact_key,
                    value: decode_json(&value_json)?,
                    status: parse_fact_status(&status)?,
                    validity: memweft_types::Validity {
                        valid_from: valid_from.map(from_millis),
                        valid_to: valid_to.map(from_millis),
                    },
                    confidence,
                    sources: decode_json(&sources)?,
                    scope_level: parse_scope_level(&scope_level)?,
                    notes,
                });
            }
            Ok(facts)
        })
    }

    fn upsert_fact(&self, scope: &Scope, fact: Fact) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO facts (
                    tenant_id, user_id, agent_id, fact_id, fact_key, value_json, status,
                    valid_from, valid_to, confidence, sources, scope_level, notes
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON DUPLICATE KEY UPDATE fact_key = VALUES(fact_key),
                                         value_json = VALUES(value_json),
                                         status = VALUES(status),
                                         valid_from = VALUES(valid_from),
                                         valid_to = VALUES(valid_to),
                                         confidence = VALUES(confidence),
                                         sources = VALUES(sources),
                                         scope_level = VALUES(scope_level),
                                         notes = VALUES(notes)",
                Params::Positional(vec![
                    MyValue::from(scope.tenant_id.clone()),
                    MyValue::from(scope.user_id.clone()),
                    MyValue::from(scope.agent_id.clone()),
                    MyValue::from(fact.fact_id),
                    MyValue::from(fact.fact_key),
                    MyValue::from(encode_json(&fact.value)?),
                    MyValue::from(fact_status_to_str(&fact.status).to_string()),
                    option_i64(option_ts(fact.validity.valid_from)),
                    option_i64(option_ts(fact.validity.valid_to)),
                    MyValue::from(fact.confidence),
                    MyValue::from(encode_json(&fact.sources)?),
                    MyValue::from(scope_level_to_str(&fact.scope_level).to_string()),
                    MyValue::from(fact.notes),
                ]),
            )
            .map_err(map_mysql_err)?;
            Ok(())
        })
    }

    fn list_episodes(&self, scope: &Scope, filter: EpisodeFilter) -> StoreResult<Vec<Episode>> {
        self.with_conn(|conn| {
            let mut use_index = !filter.tags.is_empty() || !filter.entities.is_empty();
            if !filter.tags.is_empty() && !episode_tags_present(conn, scope)? {
                use_index = false;
            }
            if !filter.entities.is_empty() && !episode_entities_present(conn, scope)? {
                use_index = false;
            }

            if use_index {
                let mut sql = String::from(
                    "SELECT episode_id, start_ts, end_ts, summary, highlights, tags, entities,
                            sources, compression_level, recency_score
                     FROM episodes WHERE tenant_id = ? AND user_id = ? AND agent_id = ?",
                );
                let mut params = scope_params_ltm(scope);

                if let Some(range) = &filter.time_range {
                    if let Some(start) = range.start {
                        sql.push_str(" AND start_ts >= ?");
                        params.push(MyValue::from(to_millis(start)));
                    }
                    if let Some(end) = range.end {
                        sql.push_str(" AND COALESCE(end_ts, start_ts) <= ?");
                        params.push(MyValue::from(to_millis(end)));
                    }
                }

                if !filter.tags.is_empty() {
                    sql.push_str(
                        " AND EXISTS (
                            SELECT 1 FROM episode_tags t
                            WHERE t.tenant_id = episodes.tenant_id
                              AND t.user_id = episodes.user_id
                              AND t.agent_id = episodes.agent_id
                              AND t.episode_id = episodes.episode_id
                              AND t.tag IN (",
                    );
                    sql.push_str(&sql_placeholders(filter.tags.len()));
                    sql.push_str("))");
                    for tag in &filter.tags {
                        params.push(MyValue::from(tag.clone()));
                    }
                }

                if !filter.entities.is_empty() {
                    sql.push_str(
                        " AND EXISTS (
                            SELECT 1 FROM episode_entities e
                            WHERE e.tenant_id = episodes.tenant_id
                              AND e.user_id = episodes.user_id
                              AND e.agent_id = episodes.agent_id
                              AND e.episode_id = episodes.episode_id
                              AND e.entity IN (",
                    );
                    sql.push_str(&sql_placeholders(filter.entities.len()));
                    sql.push_str("))");
                    for entity in &filter.entities {
                        params.push(MyValue::from(entity.clone()));
                    }
                }

                sql.push_str(" ORDER BY start_ts ASC, episode_id ASC");
                if let Some(limit) = filter.limit {
                    sql.push_str(" LIMIT ?");
                    params.push(MyValue::from(limit as i64));
                }

                let rows: Vec<mysql::Row> =
                    conn.exec(sql, Params::Positional(params))
                        .map_err(map_mysql_err)?;
                let mut episodes = Vec::with_capacity(rows.len());
                for row in rows {
                    let (
                        episode_id,
                        start_ts,
                        end_ts,
                        summary,
                        highlights,
                        tags,
                        entities,
                        sources,
                        compression_level,
                        recency_score,
                    ): (
                        String,
                        i64,
                        Option<i64>,
                        String,
                        String,
                        String,
                        String,
                        String,
                        String,
                        Option<f64>,
                    ) = from_row(row);
                    episodes.push(Episode {
                        episode_id,
                        time_range: memweft_types::TimeRange {
                            start: from_millis(start_ts),
                            end: end_ts.map(from_millis),
                        },
                        summary,
                        highlights: decode_json(&highlights)?,
                        tags: decode_json(&tags)?,
                        entities: decode_json(&entities)?,
                        sources: decode_json(&sources)?,
                        compression_level: parse_compression_level(&compression_level)?,
                        recency_score,
                    });
                }
                return Ok(episodes);
            }

            let mut sql = String::from(
                "SELECT episode_id, start_ts, end_ts, summary, highlights, tags, entities,
                        sources, compression_level, recency_score
                 FROM episodes WHERE tenant_id = ? AND user_id = ? AND agent_id = ?",
            );
            let mut params = scope_params_ltm(scope);

            if let Some(range) = &filter.time_range {
                if let Some(start) = range.start {
                    sql.push_str(" AND start_ts >= ?");
                    params.push(MyValue::from(to_millis(start)));
                }
                if let Some(end) = range.end {
                    sql.push_str(" AND COALESCE(end_ts, start_ts) <= ?");
                    params.push(MyValue::from(to_millis(end)));
                }
            }

            sql.push_str(" ORDER BY start_ts ASC, episode_id ASC");
            let limit_in_sql = if filter.tags.is_empty() && filter.entities.is_empty() {
                filter.limit
            } else {
                None
            };
            if let Some(limit) = limit_in_sql {
                sql.push_str(" LIMIT ?");
                params.push(MyValue::from(limit as i64));
            }

            let rows: Vec<mysql::Row> =
                conn.exec(sql, Params::Positional(params))
                    .map_err(map_mysql_err)?;
            let mut episodes = Vec::with_capacity(rows.len());
            for row in rows {
                let (
                    episode_id,
                    start_ts,
                    end_ts,
                    summary,
                    highlights,
                    tags,
                    entities,
                    sources,
                    compression_level,
                    recency_score,
                ): (
                    String,
                    i64,
                    Option<i64>,
                    String,
                    String,
                    String,
                    String,
                    String,
                    String,
                    Option<f64>,
                ) = from_row(row);
                episodes.push(Episode {
                    episode_id,
                    time_range: memweft_types::TimeRange {
                        start: from_millis(start_ts),
                        end: end_ts.map(from_millis),
                    },
                    summary,
                    highlights: decode_json(&highlights)?,
                    tags: decode_json(&tags)?,
                    entities: decode_json(&entities)?,
                    sources: decode_json(&sources)?,
                    compression_level: parse_compression_level(&compression_level)?,
                    recency_score,
                });
            }

            if filter.tags.is_empty() && filter.entities.is_empty() {
                return Ok(episodes);
            }

            let mut filtered = episodes
                .into_iter()
                .filter(|episode| {
                    let tags_ok = if filter.tags.is_empty() {
                        true
                    } else {
                        episode.tags.iter().any(|tag| filter.tags.contains(tag))
                    };
                    let entities_ok = if filter.entities.is_empty() {
                        true
                    } else {
                        episode
                            .entities
                            .iter()
                            .any(|entity| filter.entities.contains(entity))
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
        let episode_id = episode.episode_id.clone();
        let tags = episode.tags.clone();
        let entities = episode.entities.clone();
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO episodes (
                    tenant_id, user_id, agent_id, episode_id, start_ts, end_ts, summary,
                    highlights, tags, entities, sources, compression_level, recency_score
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                Params::Positional(vec![
                    MyValue::from(scope.tenant_id.clone()),
                    MyValue::from(scope.user_id.clone()),
                    MyValue::from(scope.agent_id.clone()),
                    MyValue::from(episode.episode_id),
                    MyValue::from(to_millis(episode.time_range.start)),
                    option_i64(option_ts(episode.time_range.end)),
                    MyValue::from(episode.summary),
                    MyValue::from(encode_json(&episode.highlights)?),
                    MyValue::from(encode_json(&episode.tags)?),
                    MyValue::from(encode_json(&episode.entities)?),
                    MyValue::from(encode_json(&episode.sources)?),
                    MyValue::from(compression_level_to_str(&episode.compression_level).to_string()),
                    option_f64(episode.recency_score),
                ]),
            )
            .map_err(map_mysql_err)?;
            insert_episode_tags(conn, scope, &episode_id, &tags, &entities)?;
            Ok(())
        })
    }

    fn list_procedures(
        &self,
        scope: &Scope,
        task_type: &str,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Procedure>> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT procedure_id, task_type, content_json, priority, sources, applicability
                 FROM procedures WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND task_type = ?",
            );
            let mut params = scope_params_ltm(scope);
            params.push(MyValue::from(task_type.to_string()));

            sql.push_str(" ORDER BY priority DESC, procedure_id ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ?");
                params.push(MyValue::from(limit as i64));
            }

            let rows: Vec<mysql::Row> =
                conn.exec(sql, Params::Positional(params))
                    .map_err(map_mysql_err)?;
            let mut procedures = Vec::with_capacity(rows.len());
            for row in rows {
                let (procedure_id, task_type, content_json, priority, sources, applicability): (
                    String,
                    String,
                    String,
                    i32,
                    String,
                    String,
                ) = from_row(row);
                procedures.push(Procedure {
                    procedure_id,
                    task_type,
                    content: decode_json(&content_json)?,
                    priority,
                    sources: decode_json(&sources)?,
                    applicability: decode_json(&applicability)?,
                });
            }
            Ok(procedures)
        })
    }

    fn upsert_procedure(&self, scope: &Scope, procedure: Procedure) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO procedures (
                    tenant_id, user_id, agent_id, procedure_id, task_type, content_json,
                    priority, sources, applicability
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON DUPLICATE KEY UPDATE task_type = VALUES(task_type),
                                         content_json = VALUES(content_json),
                                         priority = VALUES(priority),
                                         sources = VALUES(sources),
                                         applicability = VALUES(applicability)",
                (
                    scope.tenant_id.clone(),
                    scope.user_id.clone(),
                    scope.agent_id.clone(),
                    procedure.procedure_id,
                    procedure.task_type,
                    encode_json(&procedure.content)?,
                    procedure.priority,
                    encode_json(&procedure.sources)?,
                    encode_json(&procedure.applicability)?,
                ),
            )
            .map_err(map_mysql_err)?;
            Ok(())
        })
    }

    fn list_insights(&self, scope: &Scope, filter: InsightFilter) -> StoreResult<Vec<InsightItem>> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT insight_id, kind, statement, `trigger`, confidence, validation_state,
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
                        params.push(MyValue::from(validation_state_to_str(state)));
                    }
                    sql.push(')');
                }
            }

            sql.push_str(" ORDER BY validation_state DESC, confidence DESC, insight_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ?");
                params.push(MyValue::from(limit as i64));
            }

            let rows: Vec<mysql::Row> =
                conn.exec(sql, Params::Positional(params))
                    .map_err(map_mysql_err)?;
            let mut insights = Vec::with_capacity(rows.len());
            for row in rows {
                let (
                    insight_id,
                    kind,
                    statement,
                    trigger,
                    confidence,
                    validation_state,
                    tests_suggested,
                    expires_at,
                    sources,
                ): (String, String, String, String, f64, String, String, String, String) =
                    from_row(row);
                insights.push(InsightItem {
                    id: insight_id,
                    kind: parse_insight_type(&kind)?,
                    statement,
                    trigger: parse_insight_trigger(&trigger)?,
                    confidence,
                    validation_state: parse_validation_state(&validation_state)?,
                    tests_suggested: decode_json(&tests_suggested)?,
                    expires_at,
                    sources: decode_json(&sources)?,
                });
            }
            Ok(insights)
        })
    }

    fn append_insight(&self, scope: &Scope, insight: InsightItem) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO insights (
                    tenant_id, user_id, agent_id, session_id, run_id, insight_id,
                    kind, statement, `trigger`, confidence, validation_state,
                    tests_suggested, expires_at, sources
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                Params::Positional(vec![
                    MyValue::from(scope.tenant_id.clone()),
                    MyValue::from(scope.user_id.clone()),
                    MyValue::from(scope.agent_id.clone()),
                    MyValue::from(scope.session_id.clone()),
                    MyValue::from(scope.run_id.clone()),
                    MyValue::from(insight.id),
                    MyValue::from(insight_type_to_str(&insight.kind).to_string()),
                    MyValue::from(insight.statement),
                    MyValue::from(insight_trigger_to_str(&insight.trigger).to_string()),
                    MyValue::from(insight.confidence),
                    MyValue::from(validation_state_to_str(&insight.validation_state).to_string()),
                    MyValue::from(encode_json(&insight.tests_suggested)?),
                    MyValue::from(insight.expires_at),
                    MyValue::from(encode_json(&insight.sources)?),
                ]),
            )
            .map_err(map_mysql_err)?;
            Ok(())
        })
    }

    fn write_context_build(&self, scope: &Scope, packet: MemoryPacket) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.exec_drop(
                "INSERT INTO context_builds (
                    tenant_id, user_id, agent_id, session_id, run_id, ts, packet_json
                 ) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.tenant_id.clone(),
                    scope.user_id.clone(),
                    scope.agent_id.clone(),
                    scope.session_id.clone(),
                    scope.run_id.clone(),
                    to_millis(packet.meta.generated_at),
                    encode_json(&packet)?,
                ),
            )
            .map_err(map_mysql_err)?;
            Ok(())
        })
    }

    fn list_context_builds(
        &self,
        scope: &Scope,
        limit: Option<usize>,
    ) -> StoreResult<Vec<MemoryPacket>> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT packet_json FROM context_builds
                 WHERE tenant_id = ? AND user_id = ? AND agent_id = ? AND session_id = ? AND run_id = ?
                 ORDER BY ts ASC",
            );
            let mut params = scope_params(scope);
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ?");
                params.push(MyValue::from(limit as i64));
            }

            let rows: Vec<mysql::Row> =
                conn.exec(sql, Params::Positional(params))
                    .map_err(map_mysql_err)?;
            let mut packets = Vec::with_capacity(rows.len());
            for row in rows {
                let (payload,): (String,) = from_row(row);
                packets.push(decode_json(&payload)?);
            }
            Ok(packets)
        })
    }
}

fn ensure_schema(conn: &mut PooledConn) -> StoreResult<()> {
    conn.query_drop(
        "CREATE TABLE IF NOT EXISTS schema_migrations (
            version BIGINT NOT NULL PRIMARY KEY,
            applied_at BIGINT NOT NULL
        ) ENGINE=InnoDB",
    )
    .map_err(map_mysql_err)?;

    let current: Option<(i64,)> = conn
        .query_first("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
        .map_err(map_mysql_err)?;
    let current = current.map(|(version,)| version).unwrap_or(0);

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

    let schema = [
        "CREATE TABLE IF NOT EXISTS events (
            event_id VARCHAR(96) PRIMARY KEY,
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            run_id VARCHAR(96) NOT NULL,
            ts BIGINT NOT NULL,
            kind VARCHAR(32) NOT NULL,
            payload TEXT NOT NULL,
            tags TEXT NOT NULL,
            entities TEXT NOT NULL
        ) ENGINE=InnoDB",
        "CREATE INDEX events_scope_ts
            ON events (tenant_id, user_id, agent_id, session_id, run_id, ts)",
        "CREATE TABLE IF NOT EXISTS event_tags (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            run_id VARCHAR(96) NOT NULL,
            event_id VARCHAR(96) NOT NULL,
            tag VARCHAR(64) NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id, event_id, tag)
        ) ENGINE=InnoDB",
        "CREATE INDEX event_tags_scope_tag
            ON event_tags (tenant_id, user_id, agent_id, session_id, run_id, tag)",
        "CREATE TABLE IF NOT EXISTS event_entities (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            run_id VARCHAR(96) NOT NULL,
            event_id VARCHAR(96) NOT NULL,
            entity VARCHAR(64) NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id, event_id, entity)
        ) ENGINE=InnoDB",
        "CREATE INDEX event_entities_scope_entity
            ON event_entities (tenant_id, user_id, agent_id, session_id, run_id, entity)",
        "CREATE TABLE IF NOT EXISTS wm_state (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            run_id VARCHAR(96) NOT NULL,
            state_json TEXT NOT NULL,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id)
        ) ENGINE=InnoDB",
        "CREATE TABLE IF NOT EXISTS stm_state (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            rolling_summary TEXT NOT NULL,
            key_quotes TEXT NOT NULL,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id)
        ) ENGINE=InnoDB",
        "CREATE TABLE IF NOT EXISTS facts (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            fact_id VARCHAR(96) NOT NULL,
            fact_key VARCHAR(96) NOT NULL,
            value_json TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            valid_from BIGINT NULL,
            valid_to BIGINT NULL,
            confidence DOUBLE NOT NULL,
            sources TEXT NOT NULL,
            scope_level VARCHAR(32) NOT NULL,
            notes TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, fact_id)
        ) ENGINE=InnoDB",
        "CREATE INDEX facts_scope_status
            ON facts (tenant_id, user_id, agent_id, status)",
        "CREATE TABLE IF NOT EXISTS episodes (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            episode_id VARCHAR(96) NOT NULL,
            start_ts BIGINT NOT NULL,
            end_ts BIGINT NULL,
            summary TEXT NOT NULL,
            highlights TEXT NOT NULL,
            tags TEXT NOT NULL,
            entities TEXT NOT NULL,
            sources TEXT NOT NULL,
            compression_level VARCHAR(32) NOT NULL,
            recency_score DOUBLE NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, episode_id)
        ) ENGINE=InnoDB",
        "CREATE INDEX episodes_scope_start
            ON episodes (tenant_id, user_id, agent_id, start_ts)",
        "CREATE TABLE IF NOT EXISTS episode_tags (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            episode_id VARCHAR(96) NOT NULL,
            tag VARCHAR(64) NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, episode_id, tag)
        ) ENGINE=InnoDB",
        "CREATE INDEX episode_tags_scope_tag
            ON episode_tags (tenant_id, user_id, agent_id, tag)",
        "CREATE TABLE IF NOT EXISTS episode_entities (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            episode_id VARCHAR(96) NOT NULL,
            entity VARCHAR(64) NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, episode_id, entity)
        ) ENGINE=InnoDB",
        "CREATE INDEX episode_entities_scope_entity
            ON episode_entities (tenant_id, user_id, agent_id, entity)",
        "CREATE TABLE IF NOT EXISTS procedures (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            procedure_id VARCHAR(96) NOT NULL,
            task_type VARCHAR(96) NOT NULL,
            content_json TEXT NOT NULL,
            priority INT NOT NULL,
            sources TEXT NOT NULL,
            applicability TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, procedure_id)
        ) ENGINE=InnoDB",
        "CREATE INDEX procedures_scope_task
            ON procedures (tenant_id, user_id, agent_id, task_type, priority)",
        "CREATE TABLE IF NOT EXISTS insights (
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            run_id VARCHAR(96) NOT NULL,
            insight_id VARCHAR(96) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            statement TEXT NOT NULL,
            `trigger` VARCHAR(32) NOT NULL,
            confidence DOUBLE NOT NULL,
            validation_state VARCHAR(32) NOT NULL,
            tests_suggested TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            sources TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id, insight_id)
        ) ENGINE=InnoDB",
        "CREATE INDEX insights_scope_state
            ON insights (tenant_id, user_id, agent_id, session_id, run_id, validation_state)",
        "CREATE TABLE IF NOT EXISTS context_builds (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            tenant_id VARCHAR(96) NOT NULL,
            user_id VARCHAR(96) NOT NULL,
            agent_id VARCHAR(96) NOT NULL,
            session_id VARCHAR(96) NOT NULL,
            run_id VARCHAR(96) NOT NULL,
            ts BIGINT NOT NULL,
            packet_json TEXT NOT NULL
        ) ENGINE=InnoDB",
        "CREATE INDEX context_builds_scope_ts
            ON context_builds (tenant_id, user_id, agent_id, session_id, run_id, ts)",
    ];

    for statement in schema {
        apply_schema_statement(conn, statement)?;
    }

    if current == 0 {
        conn.exec_drop(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, to_millis(Utc::now())),
        )
        .map_err(map_mysql_err)?;
    }

    Ok(())
}

fn encode_json<T: Serialize>(value: &T) -> StoreResult<String> {
    Ok(serde_json::to_string(value)?)
}

fn decode_json<T: DeserializeOwned>(value: &str) -> StoreResult<T> {
    Ok(serde_json::from_str(value)?)
}

fn to_millis(ts: DateTime<Utc>) -> i64 {
    ts.timestamp_millis()
}

fn from_millis(millis: i64) -> DateTime<Utc> {
    Utc.timestamp_millis_opt(millis)
        .single()
        .unwrap_or_else(|| Utc.timestamp_millis_opt(0).single().unwrap())
}

fn option_ts(value: Option<DateTime<Utc>>) -> Option<i64> {
    value.map(to_millis)
}

fn option_i64(value: Option<i64>) -> MyValue {
    value.map(MyValue::from).unwrap_or(MyValue::NULL)
}

fn option_f64(value: Option<f64>) -> MyValue {
    value.map(MyValue::from).unwrap_or(MyValue::NULL)
}

fn scope_params(scope: &Scope) -> Vec<MyValue> {
    vec![
        MyValue::from(scope.tenant_id.clone()),
        MyValue::from(scope.user_id.clone()),
        MyValue::from(scope.agent_id.clone()),
        MyValue::from(scope.session_id.clone()),
        MyValue::from(scope.run_id.clone()),
    ]
}

fn scope_params_ltm(scope: &Scope) -> Vec<MyValue> {
    vec![
        MyValue::from(scope.tenant_id.clone()),
        MyValue::from(scope.user_id.clone()),
        MyValue::from(scope.agent_id.clone()),
    ]
}

fn sql_placeholders(count: usize) -> String {
    std::iter::repeat("?")
        .take(count)
        .collect::<Vec<_>>()
        .join(", ")
}

fn unique_values(values: &[String]) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    for value in values {
        if seen.insert(value.clone()) {
            result.push(value.clone());
        }
    }
    result
}

fn insert_event_tags(
    conn: &mut PooledConn,
    scope: &Scope,
    event_id: &str,
    tags: &[String],
    entities: &[String],
) -> StoreResult<()> {
    let tags = unique_values(tags);
    if !tags.is_empty() {
        let mut params = Vec::with_capacity(tags.len());
        for tag in tags {
            params.push(Params::Positional(vec![
                MyValue::from(scope.tenant_id.clone()),
                MyValue::from(scope.user_id.clone()),
                MyValue::from(scope.agent_id.clone()),
                MyValue::from(scope.session_id.clone()),
                MyValue::from(scope.run_id.clone()),
                MyValue::from(event_id.to_string()),
                MyValue::from(tag),
            ]));
        }
        conn.exec_batch(
            "INSERT IGNORE INTO event_tags (
                tenant_id, user_id, agent_id, session_id, run_id, event_id, tag
             ) VALUES (?, ?, ?, ?, ?, ?, ?)",
            params,
        )
        .map_err(map_mysql_err)?;
    }

    let entities = unique_values(entities);
    if !entities.is_empty() {
        let mut params = Vec::with_capacity(entities.len());
        for entity in entities {
            params.push(Params::Positional(vec![
                MyValue::from(scope.tenant_id.clone()),
                MyValue::from(scope.user_id.clone()),
                MyValue::from(scope.agent_id.clone()),
                MyValue::from(scope.session_id.clone()),
                MyValue::from(scope.run_id.clone()),
                MyValue::from(event_id.to_string()),
                MyValue::from(entity),
            ]));
        }
        conn.exec_batch(
            "INSERT IGNORE INTO event_entities (
                tenant_id, user_id, agent_id, session_id, run_id, event_id, entity
             ) VALUES (?, ?, ?, ?, ?, ?, ?)",
            params,
        )
        .map_err(map_mysql_err)?;
    }
    Ok(())
}

fn insert_event_tags_bulk(conn: &mut PooledConn, events: &[Event]) -> StoreResult<()> {
    let mut tag_params = Vec::new();
    let mut entity_params = Vec::new();
    for event in events {
        for tag in unique_values(&event.tags) {
            tag_params.push(Params::Positional(vec![
                MyValue::from(event.scope.tenant_id.clone()),
                MyValue::from(event.scope.user_id.clone()),
                MyValue::from(event.scope.agent_id.clone()),
                MyValue::from(event.scope.session_id.clone()),
                MyValue::from(event.scope.run_id.clone()),
                MyValue::from(event.event_id.clone()),
                MyValue::from(tag),
            ]));
        }
        for entity in unique_values(&event.entities) {
            entity_params.push(Params::Positional(vec![
                MyValue::from(event.scope.tenant_id.clone()),
                MyValue::from(event.scope.user_id.clone()),
                MyValue::from(event.scope.agent_id.clone()),
                MyValue::from(event.scope.session_id.clone()),
                MyValue::from(event.scope.run_id.clone()),
                MyValue::from(event.event_id.clone()),
                MyValue::from(entity),
            ]));
        }
    }
    if !tag_params.is_empty() {
        conn.exec_batch(
            "INSERT IGNORE INTO event_tags (
                tenant_id, user_id, agent_id, session_id, run_id, event_id, tag
             ) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tag_params,
        )
        .map_err(map_mysql_err)?;
    }
    if !entity_params.is_empty() {
        conn.exec_batch(
            "INSERT IGNORE INTO event_entities (
                tenant_id, user_id, agent_id, session_id, run_id, event_id, entity
             ) VALUES (?, ?, ?, ?, ?, ?, ?)",
            entity_params,
        )
        .map_err(map_mysql_err)?;
    }
    Ok(())
}

fn insert_episode_tags(
    conn: &mut PooledConn,
    scope: &Scope,
    episode_id: &str,
    tags: &[String],
    entities: &[String],
) -> StoreResult<()> {
    let tags = unique_values(tags);
    if !tags.is_empty() {
        let mut params = Vec::with_capacity(tags.len());
        for tag in tags {
            params.push(Params::Positional(vec![
                MyValue::from(scope.tenant_id.clone()),
                MyValue::from(scope.user_id.clone()),
                MyValue::from(scope.agent_id.clone()),
                MyValue::from(episode_id.to_string()),
                MyValue::from(tag),
            ]));
        }
        conn.exec_batch(
            "INSERT IGNORE INTO episode_tags (
                tenant_id, user_id, agent_id, episode_id, tag
             ) VALUES (?, ?, ?, ?, ?)",
            params,
        )
        .map_err(map_mysql_err)?;
    }

    let entities = unique_values(entities);
    if !entities.is_empty() {
        let mut params = Vec::with_capacity(entities.len());
        for entity in entities {
            params.push(Params::Positional(vec![
                MyValue::from(scope.tenant_id.clone()),
                MyValue::from(scope.user_id.clone()),
                MyValue::from(scope.agent_id.clone()),
                MyValue::from(episode_id.to_string()),
                MyValue::from(entity),
            ]));
        }
        conn.exec_batch(
            "INSERT IGNORE INTO episode_entities (
                tenant_id, user_id, agent_id, episode_id, entity
             ) VALUES (?, ?, ?, ?, ?)",
            params,
        )
        .map_err(map_mysql_err)?;
    }
    Ok(())
}

fn episode_tags_present(conn: &mut PooledConn, scope: &Scope) -> StoreResult<bool> {
    let row: Option<(u8,)> = conn
        .exec_first(
            "SELECT 1 FROM episode_tags WHERE tenant_id = ? AND user_id = ? AND agent_id = ? LIMIT 1",
            Params::Positional(scope_params_ltm(scope)),
        )
        .map_err(map_mysql_err)?;
    Ok(row.is_some())
}

fn episode_entities_present(conn: &mut PooledConn, scope: &Scope) -> StoreResult<bool> {
    let row: Option<(u8,)> = conn
        .exec_first(
            "SELECT 1 FROM episode_entities WHERE tenant_id = ? AND user_id = ? AND agent_id = ? LIMIT 1",
            Params::Positional(scope_params_ltm(scope)),
        )
        .map_err(map_mysql_err)?;
    Ok(row.is_some())
}

fn event_kind_to_str(kind: &EventKind) -> &'static str {
    match kind {
        EventKind::Message => "message",
        EventKind::ToolResult => "tool_result",
        EventKind::StatePatch => "state_patch",
        EventKind::System => "system",
    }
}

fn parse_event_kind(value: &str) -> StoreResult<EventKind> {
    match value {
        "message" => Ok(EventKind::Message),
        "tool_result" => Ok(EventKind::ToolResult),
        "state_patch" => Ok(EventKind::StatePatch),
        "system" => Ok(EventKind::System),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid event kind: {}",
            value
        ))),
    }
}

fn fact_status_to_str(status: &FactStatus) -> &'static str {
    match status {
        FactStatus::Active => "active",
        FactStatus::Disputed => "disputed",
        FactStatus::Deprecated => "deprecated",
    }
}

fn parse_fact_status(value: &str) -> StoreResult<FactStatus> {
    match value {
        "active" => Ok(FactStatus::Active),
        "disputed" => Ok(FactStatus::Disputed),
        "deprecated" => Ok(FactStatus::Deprecated),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid fact status: {}",
            value
        ))),
    }
}

fn scope_level_to_str(level: &ScopeLevel) -> &'static str {
    match level {
        ScopeLevel::User => "user",
        ScopeLevel::Agent => "agent",
        ScopeLevel::Tenant => "tenant",
    }
}

fn parse_scope_level(value: &str) -> StoreResult<ScopeLevel> {
    match value {
        "user" => Ok(ScopeLevel::User),
        "agent" => Ok(ScopeLevel::Agent),
        "tenant" => Ok(ScopeLevel::Tenant),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid scope level: {}",
            value
        ))),
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

fn parse_compression_level(value: &str) -> StoreResult<CompressionLevel> {
    match value {
        "raw" => Ok(CompressionLevel::Raw),
        "phase_summary" => Ok(CompressionLevel::PhaseSummary),
        "milestone" => Ok(CompressionLevel::Milestone),
        "theme" => Ok(CompressionLevel::Theme),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid compression level: {}",
            value
        ))),
    }
}

fn insight_type_to_str(kind: &InsightType) -> &'static str {
    match kind {
        InsightType::Hypothesis => "hypothesis",
        InsightType::Strategy => "strategy",
        InsightType::Pattern => "pattern",
    }
}

fn parse_insight_type(value: &str) -> StoreResult<InsightType> {
    match value {
        "hypothesis" => Ok(InsightType::Hypothesis),
        "strategy" => Ok(InsightType::Strategy),
        "pattern" => Ok(InsightType::Pattern),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid insight type: {}",
            value
        ))),
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

fn parse_insight_trigger(value: &str) -> StoreResult<InsightTrigger> {
    match value {
        "conflict" => Ok(InsightTrigger::Conflict),
        "failure" => Ok(InsightTrigger::Failure),
        "synthesis" => Ok(InsightTrigger::Synthesis),
        "analogy" => Ok(InsightTrigger::Analogy),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid insight trigger: {}",
            value
        ))),
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

fn parse_validation_state(value: &str) -> StoreResult<ValidationState> {
    match value {
        "unvalidated" => Ok(ValidationState::Unvalidated),
        "testing" => Ok(ValidationState::Testing),
        "validated" => Ok(ValidationState::Validated),
        "rejected" => Ok(ValidationState::Rejected),
        _ => Err(StoreError::InvalidInput(format!(
            "invalid validation state: {}",
            value
        ))),
    }
}

fn map_mysql_err(err: mysql::Error) -> StoreError {
    StoreError::Storage(err.to_string())
}

fn ensure_mysql_database(opts: &Opts, db_name: &str) -> StoreResult<()> {
    let admin_opts = OptsBuilder::from_opts(opts.clone()).db_name(None::<String>);
    let pool = Pool::new(admin_opts).map_err(map_mysql_err)?;
    let mut conn = pool.get_conn().map_err(map_mysql_err)?;
    let statement = format!(
        "CREATE DATABASE IF NOT EXISTS `{}`",
        escape_mysql_identifier(db_name)
    );
    conn.query_drop(statement).map_err(map_mysql_err)?;
    Ok(())
}

fn apply_schema_statement(conn: &mut PooledConn, statement: &str) -> StoreResult<()> {
    match conn.query_drop(statement) {
        Ok(()) => Ok(()),
        Err(err) if is_duplicate_index(&err) => Ok(()),
        Err(err) => Err(map_mysql_err(err)),
    }
}

fn is_duplicate_index(err: &mysql::Error) -> bool {
    match err {
        mysql::Error::MySqlError(inner) => inner.code == 1061,
        _ => false,
    }
}

fn is_unknown_database(err: &mysql::Error) -> bool {
    match err {
        mysql::Error::MySqlError(inner) => inner.code == 1049,
        _ => false,
    }
}

fn escape_mysql_identifier(value: &str) -> String {
    value.replace('`', "``")
}

#[cfg(test)]
mod tests {
    use super::*;
    use memweft_types::{
        Budget, BudgetReport, Insight, JsonMap, LongTerm, MemoryPacket, Meta, Purpose, ShortTerm,
        Validity,
    };
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_id(prefix: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        format!("{}-{}", prefix, nanos)
    }

    fn sample_scope() -> Scope {
        let suffix = unique_id("scope");
        Scope {
            tenant_id: format!("tenant-{}", suffix),
            user_id: format!("user-{}", suffix),
            agent_id: format!("agent-{}", suffix),
            session_id: format!("session-{}", suffix),
            run_id: format!("run-{}", suffix),
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
            long_term: LongTerm::default(),
            insight: Insight::default(),
            citations: Vec::new(),
            budget_report: BudgetReport::default(),
            explain: JsonMap::new(),
        }
    }

    #[test]
    fn mysql_store_roundtrip() {
        let dsn = match std::env::var("MEMWEFT_MYSQL_DSN") {
            Ok(value) if !value.trim().is_empty() => value,
            _ => {
                eprintln!("MEMWEFT_MYSQL_DSN not set; skipping mysql_store_roundtrip");
                return;
            }
        };

        let store = MySqlStore::new(&dsn).unwrap();
        let scope = sample_scope();

        let event_id = unique_id("event");
        store
            .append_event(Event {
                event_id: event_id.clone(),
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
        assert_eq!(events[0].event_id, event_id);

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
                    fact_id: unique_id("f1"),
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
                    fact_id: unique_id("f2"),
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
                    episode_id: unique_id("ep1"),
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
                    episode_id: unique_id("ep2"),
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
                    procedure_id: unique_id("p1"),
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
                    id: unique_id("i1"),
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

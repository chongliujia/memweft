use chrono::{DateTime, TimeZone, Utc};
use memweft_types::{
    CompressionLevel, Episode, Fact, FactStatus, InsightItem, InsightTrigger, InsightType,
    MemoryPacket, Procedure, Scope, ScopeLevel, ValidationState, WorkingState,
};
use postgres::types::ToSql;
use postgres::{Client, NoTls};
use r2d2::Pool;
use r2d2_postgres::PostgresConnectionManager;
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::collections::HashSet;

use crate::{
    EpisodeFilter, Event, EventKind, FactFilter, InsightFilter, StmState, Store, StoreError,
    StoreResult, TimeRangeFilter, WorkingStatePatch,
};

const SCHEMA_VERSION: i64 = 1;

pub struct PostgresStore {
    pool: Pool<PostgresConnectionManager<NoTls>>,
}

impl std::fmt::Debug for PostgresStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PostgresStore").finish()
    }
}

impl PostgresStore {
    pub fn new(dsn: &str) -> StoreResult<Self> {
        Self::with_pool_size(dsn, 10)
    }

    pub fn with_pool_size(dsn: &str, max_size: u32) -> StoreResult<Self> {
        let (normalized_dsn, db_name) = normalize_postgres_dsn(dsn)?;
        ensure_postgres_database(&normalized_dsn, &db_name)?;
        let manager =
            PostgresConnectionManager::new(normalized_dsn.parse().map_err(map_pg_err)?, NoTls);
        let pool = Pool::builder()
            .max_size(max_size)
            .build(manager)
            .map_err(|err| StoreError::Storage(err.to_string()))?;
        let store = Self { pool };
        store.with_conn(|conn| ensure_schema(conn))?;
        Ok(store)
    }

    fn with_conn<F, T>(&self, f: F) -> StoreResult<T>
    where
        F: FnOnce(&mut Client) -> StoreResult<T>,
    {
        let mut conn = self
            .pool
            .get()
            .map_err(|err| StoreError::Storage(err.to_string()))?;
        f(&mut conn)
    }

    pub fn append_events_bulk(&self, events: &[Event]) -> StoreResult<()> {
        if events.is_empty() {
            return Ok(());
        }
        self.with_conn(|conn| {
            let mut tx = conn.transaction().map_err(map_pg_err)?;
            let stmt_event = tx
                .prepare(
                    "INSERT INTO events (
                        event_id, tenant_id, user_id, agent_id, session_id, run_id,
                        ts, kind, payload, tags, entities
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                )
                .map_err(map_pg_err)?;
            let stmt_tag = tx
                .prepare(
                    "INSERT INTO event_tags (
                        tenant_id, user_id, agent_id, session_id, run_id, event_id, tag
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT DO NOTHING",
                )
                .map_err(map_pg_err)?;
            let stmt_entity = tx
                .prepare(
                    "INSERT INTO event_entities (
                        tenant_id, user_id, agent_id, session_id, run_id, event_id, entity
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT DO NOTHING",
                )
                .map_err(map_pg_err)?;

            for event in events {
                tx.execute(
                    &stmt_event,
                    &[
                        &event.event_id,
                        &event.scope.tenant_id,
                        &event.scope.user_id,
                        &event.scope.agent_id,
                        &event.scope.session_id,
                        &event.scope.run_id,
                        &to_millis(event.ts),
                        &event_kind_to_str(&event.kind),
                        &encode_json(&event.payload)?,
                        &encode_json(&event.tags)?,
                        &encode_json(&event.entities)?,
                    ],
                )
                .map_err(map_pg_err)?;

                for tag in unique_values(&event.tags) {
                    tx.execute(
                        &stmt_tag,
                        &[
                            &event.scope.tenant_id,
                            &event.scope.user_id,
                            &event.scope.agent_id,
                            &event.scope.session_id,
                            &event.scope.run_id,
                            &event.event_id,
                            &tag,
                        ],
                    )
                    .map_err(map_pg_err)?;
                }
                for entity in unique_values(&event.entities) {
                    tx.execute(
                        &stmt_entity,
                        &[
                            &event.scope.tenant_id,
                            &event.scope.user_id,
                            &event.scope.agent_id,
                            &event.scope.session_id,
                            &event.scope.run_id,
                            &event.event_id,
                            &entity,
                        ],
                    )
                    .map_err(map_pg_err)?;
                }
            }

            tx.commit().map_err(map_pg_err)?;
            Ok(())
        })
    }
}

impl Store for PostgresStore {
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
            conn.execute(
                "INSERT INTO events (
                    event_id, tenant_id, user_id, agent_id, session_id, run_id,
                    ts, kind, payload, tags, entities
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                &[
                    &event_id,
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &scope.run_id,
                    &to_millis(ts),
                    &event_kind_to_str(&kind),
                    &encode_json(&payload)?,
                    &encode_json(&tags)?,
                    &encode_json(&entities)?,
                ],
            )
            .map_err(map_pg_err)?;
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
            let mut params = PgParams::new();
            let mut sql = String::from(
                "SELECT event_id, tenant_id, user_id, agent_id, session_id, run_id, ts, kind, payload, tags, entities
                 FROM events WHERE tenant_id = ",
            );
            sql.push_str(&params.add(scope.tenant_id.clone()));
            sql.push_str(" AND user_id = ");
            sql.push_str(&params.add(scope.user_id.clone()));
            sql.push_str(" AND agent_id = ");
            sql.push_str(&params.add(scope.agent_id.clone()));
            sql.push_str(" AND session_id = ");
            sql.push_str(&params.add(scope.session_id.clone()));
            sql.push_str(" AND run_id = ");
            sql.push_str(&params.add(scope.run_id.clone()));

            if let Some(start) = range.start {
                sql.push_str(" AND ts >= ");
                sql.push_str(&params.add(to_millis(start)));
            }
            if let Some(end) = range.end {
                sql.push_str(" AND ts <= ");
                sql.push_str(&params.add(to_millis(end)));
            }
            sql.push_str(" ORDER BY ts ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ");
                sql.push_str(&params.add(limit as i64));
            }

            let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
            let mut events = Vec::new();
            for row in rows {
                let kind: String = row.get(7);
                let payload: String = row.get(8);
                let tags: String = row.get(9);
                let entities: String = row.get(10);
                events.push(Event {
                    event_id: row.get(0),
                    scope: Scope {
                        tenant_id: row.get(1),
                        user_id: row.get(2),
                        agent_id: row.get(3),
                        session_id: row.get(4),
                        run_id: row.get(5),
                    },
                    ts: from_millis(row.get(6)),
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
            let rows = conn
                .query(
                    "SELECT state_json FROM wm_state
                     WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 AND session_id=$4 AND run_id=$5",
                    &[
                        &scope.tenant_id,
                        &scope.user_id,
                        &scope.agent_id,
                        &scope.session_id,
                        &scope.run_id,
                    ],
                )
                .map_err(map_pg_err)?;
            if let Some(row) = rows.first() {
                let payload: String = row.get(0);
                Ok(Some(decode_json(&payload)?))
            } else {
                Ok(None)
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
            conn.execute(
                "INSERT INTO wm_state (
                    tenant_id, user_id, agent_id, session_id, run_id, state_json, updated_at
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                 ON CONFLICT (tenant_id, user_id, agent_id, session_id, run_id)
                 DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &scope.run_id,
                    &encode_json(&next)?,
                    &to_millis(Utc::now()),
                ],
            )
            .map_err(map_pg_err)?;
            Ok(next)
        })
    }

    fn get_stm(&self, scope: &Scope) -> StoreResult<Option<StmState>> {
        self.with_conn(|conn| {
            let rows = conn
                .query(
                    "SELECT rolling_summary, key_quotes FROM stm_state
                     WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 AND session_id=$4",
                    &[&scope.tenant_id, &scope.user_id, &scope.agent_id, &scope.session_id],
                )
                .map_err(map_pg_err)?;
            if let Some(row) = rows.first() {
                let summary: String = row.get(0);
                let key_quotes: String = row.get(1);
                Ok(Some(StmState {
                    rolling_summary: summary,
                    key_quotes: decode_json(&key_quotes)?,
                }))
            } else {
                Ok(None)
            }
        })
    }

    fn update_stm(&self, scope: &Scope, stm: StmState) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO stm_state (
                    tenant_id, user_id, agent_id, session_id, rolling_summary, key_quotes, updated_at
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                 ON CONFLICT (tenant_id, user_id, agent_id, session_id)
                 DO UPDATE SET rolling_summary=excluded.rolling_summary,
                               key_quotes=excluded.key_quotes,
                               updated_at=excluded.updated_at",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &stm.rolling_summary,
                    &encode_json(&stm.key_quotes)?,
                    &to_millis(Utc::now()),
                ],
            )
            .map_err(map_pg_err)?;
            Ok(())
        })
    }

    fn list_facts(&self, scope: &Scope, filter: FactFilter) -> StoreResult<Vec<Fact>> {
        self.with_conn(|conn| {
            let mut params = PgParams::new();
            let mut sql = String::from(
                "SELECT fact_id, fact_key, value_json, status, valid_from, valid_to,
                        confidence, sources, scope_level, notes
                 FROM facts WHERE tenant_id = ",
            );
            sql.push_str(&params.add(scope.tenant_id.clone()));
            sql.push_str(" AND user_id = ");
            sql.push_str(&params.add(scope.user_id.clone()));
            sql.push_str(" AND agent_id = ");
            sql.push_str(&params.add(scope.agent_id.clone()));

            if let Some(statuses) = &filter.status {
                if !statuses.is_empty() {
                    sql.push_str(" AND status IN (");
                    for (idx, status) in statuses.iter().enumerate() {
                        if idx > 0 {
                            sql.push_str(", ");
                        }
                        sql.push_str(&params.add(fact_status_to_str(status).to_string()));
                    }
                    sql.push(')');
                }
            }

            if let Some(at) = filter.valid_at {
                let ts = to_millis(at);
                sql.push_str(" AND (valid_from IS NULL OR valid_from <= ");
                sql.push_str(&params.add(ts));
                sql.push_str(") AND (valid_to IS NULL OR valid_to >= ");
                sql.push_str(&params.add(ts));
                sql.push(')');
            }

            sql.push_str(" ORDER BY fact_key ASC, fact_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ");
                sql.push_str(&params.add(limit as i64));
            }

            let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
            let mut facts = Vec::new();
            for row in rows {
                let value_json: String = row.get(2);
                let status: String = row.get(3);
                let sources: String = row.get(7);
                let scope_level: String = row.get(8);
                facts.push(Fact {
                    fact_id: row.get(0),
                    fact_key: row.get(1),
                    value: decode_json(&value_json)?,
                    status: parse_fact_status(&status)?,
                    validity: memweft_types::Validity {
                        valid_from: row.get::<_, Option<i64>>(4).map(from_millis),
                        valid_to: row.get::<_, Option<i64>>(5).map(from_millis),
                    },
                    confidence: row.get(6),
                    sources: decode_json(&sources)?,
                    scope_level: parse_scope_level(&scope_level)?,
                    notes: row.get(9),
                });
            }
            Ok(facts)
        })
    }

    fn upsert_fact(&self, scope: &Scope, fact: Fact) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO facts (
                    tenant_id, user_id, agent_id, fact_id, fact_key, value_json, status,
                    valid_from, valid_to, confidence, sources, scope_level, notes
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                 ON CONFLICT (tenant_id, user_id, agent_id, fact_id)
                 DO UPDATE SET fact_key=excluded.fact_key,
                               value_json=excluded.value_json,
                               status=excluded.status,
                               valid_from=excluded.valid_from,
                               valid_to=excluded.valid_to,
                               confidence=excluded.confidence,
                               sources=excluded.sources,
                               scope_level=excluded.scope_level,
                               notes=excluded.notes",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &fact.fact_id,
                    &fact.fact_key,
                    &encode_json(&fact.value)?,
                    &fact_status_to_str(&fact.status),
                    &fact.validity.valid_from.map(to_millis),
                    &fact.validity.valid_to.map(to_millis),
                    &fact.confidence,
                    &encode_json(&fact.sources)?,
                    &scope_level_to_str(&fact.scope_level),
                    &fact.notes,
                ],
            )
            .map_err(map_pg_err)?;
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
                let mut params = PgParams::new();
                let mut sql = String::from(
                    "SELECT episode_id, start_ts, end_ts, summary, highlights, tags, entities,
                            sources, compression_level, recency_score
                     FROM episodes WHERE tenant_id = ",
                );
                sql.push_str(&params.add(scope.tenant_id.clone()));
                sql.push_str(" AND user_id = ");
                sql.push_str(&params.add(scope.user_id.clone()));
                sql.push_str(" AND agent_id = ");
                sql.push_str(&params.add(scope.agent_id.clone()));

                if let Some(range) = &filter.time_range {
                    if let Some(start) = range.start {
                        sql.push_str(" AND start_ts >= ");
                        sql.push_str(&params.add(to_millis(start)));
                    }
                    if let Some(end) = range.end {
                        sql.push_str(" AND COALESCE(end_ts, start_ts) <= ");
                        sql.push_str(&params.add(to_millis(end)));
                    }
                }

                if !filter.tags.is_empty() {
                    let placeholders = filter
                        .tags
                        .iter()
                        .map(|tag| params.add(tag.clone()))
                        .collect::<Vec<_>>()
                        .join(", ");
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
                }

                if !filter.entities.is_empty() {
                    let placeholders = filter
                        .entities
                        .iter()
                        .map(|entity| params.add(entity.clone()))
                        .collect::<Vec<_>>()
                        .join(", ");
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
                }

                sql.push_str(" ORDER BY start_ts ASC, episode_id ASC");
                if let Some(limit) = filter.limit {
                    sql.push_str(" LIMIT ");
                    sql.push_str(&params.add(limit as i64));
                }

                let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
                let mut episodes = Vec::new();
                for row in rows {
                    let highlights: String = row.get(4);
                    let tags: String = row.get(5);
                    let entities: String = row.get(6);
                    let sources: String = row.get(7);
                    let compression_level: String = row.get(8);
                    episodes.push(Episode {
                        episode_id: row.get(0),
                        time_range: memweft_types::TimeRange {
                            start: from_millis(row.get(1)),
                            end: row.get::<_, Option<i64>>(2).map(from_millis),
                        },
                        summary: row.get(3),
                        highlights: decode_json(&highlights)?,
                        tags: decode_json(&tags)?,
                        entities: decode_json(&entities)?,
                        sources: decode_json(&sources)?,
                        compression_level: parse_compression_level(&compression_level)?,
                        recency_score: row.get(9),
                    });
                }
                return Ok(episodes);
            }

            let mut params = PgParams::new();
            let mut sql = String::from(
                "SELECT episode_id, start_ts, end_ts, summary, highlights, tags, entities,
                        sources, compression_level, recency_score
                 FROM episodes WHERE tenant_id = ",
            );
            sql.push_str(&params.add(scope.tenant_id.clone()));
            sql.push_str(" AND user_id = ");
            sql.push_str(&params.add(scope.user_id.clone()));
            sql.push_str(" AND agent_id = ");
            sql.push_str(&params.add(scope.agent_id.clone()));

            if let Some(range) = &filter.time_range {
                if let Some(start) = range.start {
                    sql.push_str(" AND start_ts >= ");
                    sql.push_str(&params.add(to_millis(start)));
                }
                if let Some(end) = range.end {
                    sql.push_str(" AND COALESCE(end_ts, start_ts) <= ");
                    sql.push_str(&params.add(to_millis(end)));
                }
            }

            sql.push_str(" ORDER BY start_ts ASC, episode_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ");
                sql.push_str(&params.add(limit as i64));
            }

            let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
            let mut episodes = Vec::new();
            for row in rows {
                let highlights: String = row.get(4);
                let tags: String = row.get(5);
                let entities: String = row.get(6);
                let sources: String = row.get(7);
                let compression_level: String = row.get(8);
                episodes.push(Episode {
                    episode_id: row.get(0),
                    time_range: memweft_types::TimeRange {
                        start: from_millis(row.get(1)),
                        end: row.get::<_, Option<i64>>(2).map(from_millis),
                    },
                    summary: row.get(3),
                    highlights: decode_json(&highlights)?,
                    tags: decode_json(&tags)?,
                    entities: decode_json(&entities)?,
                    sources: decode_json(&sources)?,
                    compression_level: parse_compression_level(&compression_level)?,
                    recency_score: row.get(9),
                });
            }

            if filter.tags.is_empty() && filter.entities.is_empty() {
                return Ok(episodes);
            }

            let filtered = episodes
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
                .collect();

            Ok(filtered)
        })
    }

    fn append_episode(&self, scope: &Scope, episode: Episode) -> StoreResult<()> {
        let episode_id = episode.episode_id.clone();
        let tags = episode.tags.clone();
        let entities = episode.entities.clone();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO episodes (
                    tenant_id, user_id, agent_id, episode_id, start_ts, end_ts, summary,
                    highlights, tags, entities, sources, compression_level, recency_score
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &episode.episode_id,
                    &to_millis(episode.time_range.start),
                    &episode.time_range.end.map(to_millis),
                    &episode.summary,
                    &encode_json(&episode.highlights)?,
                    &encode_json(&episode.tags)?,
                    &encode_json(&episode.entities)?,
                    &encode_json(&episode.sources)?,
                    &compression_level_to_str(&episode.compression_level),
                    &episode.recency_score,
                ],
            )
            .map_err(map_pg_err)?;
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
            let mut params = PgParams::new();
            let mut sql = String::from(
                "SELECT procedure_id, task_type, content_json, priority, sources, applicability
                 FROM procedures WHERE tenant_id = ",
            );
            sql.push_str(&params.add(scope.tenant_id.clone()));
            sql.push_str(" AND user_id = ");
            sql.push_str(&params.add(scope.user_id.clone()));
            sql.push_str(" AND agent_id = ");
            sql.push_str(&params.add(scope.agent_id.clone()));
            sql.push_str(" AND task_type = ");
            sql.push_str(&params.add(task_type.to_string()));
            sql.push_str(" ORDER BY priority DESC, procedure_id ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ");
                sql.push_str(&params.add(limit as i64));
            }

            let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
            let mut procedures = Vec::new();
            for row in rows {
                let content: String = row.get(2);
                let sources: String = row.get(4);
                let applicability: String = row.get(5);
                procedures.push(Procedure {
                    procedure_id: row.get(0),
                    task_type: row.get(1),
                    content: decode_json(&content)?,
                    priority: row.get(3),
                    sources: decode_json(&sources)?,
                    applicability: decode_json(&applicability)?,
                });
            }
            Ok(procedures)
        })
    }

    fn upsert_procedure(&self, scope: &Scope, procedure: Procedure) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO procedures (
                    tenant_id, user_id, agent_id, procedure_id, task_type, content_json,
                    priority, sources, applicability
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                 ON CONFLICT (tenant_id, user_id, agent_id, procedure_id)
                 DO UPDATE SET task_type=excluded.task_type,
                               content_json=excluded.content_json,
                               priority=excluded.priority,
                               sources=excluded.sources,
                               applicability=excluded.applicability",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &procedure.procedure_id,
                    &procedure.task_type,
                    &encode_json(&procedure.content)?,
                    &procedure.priority,
                    &encode_json(&procedure.sources)?,
                    &encode_json(&procedure.applicability)?,
                ],
            )
            .map_err(map_pg_err)?;
            Ok(())
        })
    }

    fn list_insights(&self, scope: &Scope, filter: InsightFilter) -> StoreResult<Vec<InsightItem>> {
        self.with_conn(|conn| {
            let mut params = PgParams::new();
            let mut sql = String::from(
                "SELECT insight_id, kind, statement, trigger, confidence, validation_state,
                        tests_suggested, expires_at, sources
                 FROM insights WHERE tenant_id = ",
            );
            sql.push_str(&params.add(scope.tenant_id.clone()));
            sql.push_str(" AND user_id = ");
            sql.push_str(&params.add(scope.user_id.clone()));
            sql.push_str(" AND agent_id = ");
            sql.push_str(&params.add(scope.agent_id.clone()));
            sql.push_str(" AND session_id = ");
            sql.push_str(&params.add(scope.session_id.clone()));
            sql.push_str(" AND run_id = ");
            sql.push_str(&params.add(scope.run_id.clone()));

            if let Some(states) = &filter.validation_state {
                if !states.is_empty() {
                    sql.push_str(" AND validation_state IN (");
                    for (idx, state) in states.iter().enumerate() {
                        if idx > 0 {
                            sql.push_str(", ");
                        }
                        sql.push_str(&params.add(validation_state_to_str(state).to_string()));
                    }
                    sql.push(')');
                }
            }

            sql.push_str(" ORDER BY validation_state DESC, confidence DESC, insight_id ASC");
            if let Some(limit) = filter.limit {
                sql.push_str(" LIMIT ");
                sql.push_str(&params.add(limit as i64));
            }

            let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
            let mut insights = Vec::new();
            for row in rows {
                let kind: String = row.get(1);
                let trigger: String = row.get(3);
                let validation_state: String = row.get(5);
                let tests: String = row.get(6);
                let sources: String = row.get(8);
                insights.push(InsightItem {
                    id: row.get(0),
                    kind: parse_insight_type(&kind)?,
                    statement: row.get(2),
                    trigger: parse_insight_trigger(&trigger)?,
                    confidence: row.get(4),
                    validation_state: parse_validation_state(&validation_state)?,
                    tests_suggested: decode_json(&tests)?,
                    expires_at: row.get(7),
                    sources: decode_json(&sources)?,
                });
            }
            Ok(insights)
        })
    }

    fn append_insight(&self, scope: &Scope, insight: InsightItem) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO insights (
                    tenant_id, user_id, agent_id, session_id, run_id, insight_id,
                    kind, statement, trigger, confidence, validation_state,
                    tests_suggested, expires_at, sources
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &scope.run_id,
                    &insight.id,
                    &insight_type_to_str(&insight.kind),
                    &insight.statement,
                    &insight_trigger_to_str(&insight.trigger),
                    &insight.confidence,
                    &validation_state_to_str(&insight.validation_state),
                    &encode_json(&insight.tests_suggested)?,
                    &insight.expires_at,
                    &encode_json(&insight.sources)?,
                ],
            )
            .map_err(map_pg_err)?;
            Ok(())
        })
    }

    fn write_context_build(&self, scope: &Scope, packet: MemoryPacket) -> StoreResult<()> {
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO context_builds (
                    tenant_id, user_id, agent_id, session_id, run_id, ts, packet_json
                 ) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &scope.run_id,
                    &to_millis(packet.meta.generated_at),
                    &encode_json(&packet)?,
                ],
            )
            .map_err(map_pg_err)?;
            Ok(())
        })
    }

    fn list_context_builds(
        &self,
        scope: &Scope,
        limit: Option<usize>,
    ) -> StoreResult<Vec<MemoryPacket>> {
        self.with_conn(|conn| {
            let mut params = PgParams::new();
            let mut sql = String::from(
                "SELECT packet_json FROM context_builds
                 WHERE tenant_id = ",
            );
            sql.push_str(&params.add(scope.tenant_id.clone()));
            sql.push_str(" AND user_id = ");
            sql.push_str(&params.add(scope.user_id.clone()));
            sql.push_str(" AND agent_id = ");
            sql.push_str(&params.add(scope.agent_id.clone()));
            sql.push_str(" AND session_id = ");
            sql.push_str(&params.add(scope.session_id.clone()));
            sql.push_str(" AND run_id = ");
            sql.push_str(&params.add(scope.run_id.clone()));
            sql.push_str(" ORDER BY ts ASC");
            if let Some(limit) = limit {
                sql.push_str(" LIMIT ");
                sql.push_str(&params.add(limit as i64));
            }

            let rows = conn.query(&sql, &params.refs()).map_err(map_pg_err)?;
            let mut packets = Vec::new();
            for row in rows {
                let payload: String = row.get(0);
                packets.push(decode_json(&payload)?);
            }
            Ok(packets)
        })
    }
}

fn ensure_schema(conn: &mut Client) -> StoreResult<()> {
    conn.batch_execute(
        "
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version BIGINT NOT NULL,
            applied_at BIGINT NOT NULL
        );
        ",
    )
    .map_err(map_pg_err)?;

    let row = conn
        .query_opt(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1",
            &[],
        )
        .map_err(map_pg_err)?;
    let current = row.map(|row| row.get::<_, i64>(0)).unwrap_or(0);

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

    conn.batch_execute(
        "
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            ts BIGINT NOT NULL,
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
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id)
        );

        CREATE TABLE IF NOT EXISTS stm_state (
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            rolling_summary TEXT NOT NULL,
            key_quotes TEXT NOT NULL,
            updated_at BIGINT NOT NULL,
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
            valid_from BIGINT,
            valid_to BIGINT,
            confidence DOUBLE PRECISION NOT NULL,
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
            start_ts BIGINT NOT NULL,
            end_ts BIGINT,
            summary TEXT NOT NULL,
            highlights TEXT NOT NULL,
            tags TEXT NOT NULL,
            entities TEXT NOT NULL,
            sources TEXT NOT NULL,
            compression_level TEXT NOT NULL,
            recency_score DOUBLE PRECISION,
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
            confidence DOUBLE PRECISION NOT NULL,
            validation_state TEXT NOT NULL,
            tests_suggested TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            sources TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, agent_id, session_id, run_id, insight_id)
        );
        CREATE INDEX IF NOT EXISTS insights_scope_state
            ON insights (tenant_id, user_id, agent_id, session_id, run_id, validation_state);

        CREATE TABLE IF NOT EXISTS context_builds (
            id BIGSERIAL PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            ts BIGINT NOT NULL,
            packet_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS context_builds_scope_ts
            ON context_builds (tenant_id, user_id, agent_id, session_id, run_id, ts);
        ",
    )
    .map_err(map_pg_err)?;

    if current == 0 {
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES ($1,$2)",
            &[&SCHEMA_VERSION, &to_millis(Utc::now())],
        )
        .map_err(map_pg_err)?;
    }

    Ok(())
}

struct PgParams {
    values: Vec<Box<dyn ToSql + Sync>>,
}

impl PgParams {
    fn new() -> Self {
        Self { values: Vec::new() }
    }

    fn add<T: ToSql + Sync + 'static>(&mut self, value: T) -> String {
        self.values.push(Box::new(value));
        format!("${}", self.values.len())
    }

    fn refs(&self) -> Vec<&(dyn ToSql + Sync)> {
        self.values
            .iter()
            .map(|value| &**value as &(dyn ToSql + Sync))
            .collect()
    }
}

fn encode_json<T: Serialize>(value: &T) -> StoreResult<String> {
    Ok(serde_json::to_string(value).map_err(|err| StoreError::InvalidInput(err.to_string()))?)
}

fn decode_json<T: DeserializeOwned>(value: &str) -> StoreResult<T> {
    Ok(serde_json::from_str(value).map_err(|err| StoreError::InvalidInput(err.to_string()))?)
}

fn to_millis(ts: DateTime<Utc>) -> i64 {
    ts.timestamp_millis()
}

fn from_millis(millis: i64) -> DateTime<Utc> {
    Utc.timestamp_millis_opt(millis)
        .single()
        .unwrap_or_else(|| Utc.timestamp_millis_opt(0).single().unwrap())
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

fn insert_event_tags(
    conn: &mut Client,
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
        let stmt = conn
            .prepare(
                "INSERT INTO event_tags (
                    tenant_id, user_id, agent_id, session_id, run_id, event_id, tag
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT DO NOTHING",
            )
            .map_err(map_pg_err)?;
        for tag in tags {
            conn.execute(
                &stmt,
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &scope.run_id,
                    &event_id,
                    &tag,
                ],
            )
            .map_err(map_pg_err)?;
        }
    }

    if !entities.is_empty() {
        let stmt = conn
            .prepare(
                "INSERT INTO event_entities (
                    tenant_id, user_id, agent_id, session_id, run_id, event_id, entity
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT DO NOTHING",
            )
            .map_err(map_pg_err)?;
        for entity in entities {
            conn.execute(
                &stmt,
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &scope.session_id,
                    &scope.run_id,
                    &event_id,
                    &entity,
                ],
            )
            .map_err(map_pg_err)?;
        }
    }

    Ok(())
}

fn insert_episode_tags(
    conn: &mut Client,
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
        let stmt = conn
            .prepare(
                "INSERT INTO episode_tags (
                    tenant_id, user_id, agent_id, episode_id, tag
                ) VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT DO NOTHING",
            )
            .map_err(map_pg_err)?;
        for tag in tags {
            conn.execute(
                &stmt,
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &episode_id,
                    &tag,
                ],
            )
            .map_err(map_pg_err)?;
        }
    }

    if !entities.is_empty() {
        let stmt = conn
            .prepare(
                "INSERT INTO episode_entities (
                    tenant_id, user_id, agent_id, episode_id, entity
                ) VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT DO NOTHING",
            )
            .map_err(map_pg_err)?;
        for entity in entities {
            conn.execute(
                &stmt,
                &[
                    &scope.tenant_id,
                    &scope.user_id,
                    &scope.agent_id,
                    &episode_id,
                    &entity,
                ],
            )
            .map_err(map_pg_err)?;
        }
    }

    Ok(())
}

fn episode_tags_present(conn: &mut Client, scope: &Scope) -> StoreResult<bool> {
    let row = conn
        .query_opt(
            "SELECT 1 FROM episode_tags WHERE tenant_id = $1 AND user_id = $2 AND agent_id = $3 LIMIT 1",
            &[&scope.tenant_id, &scope.user_id, &scope.agent_id],
        )
        .map_err(map_pg_err)?;
    Ok(row.is_some())
}

fn episode_entities_present(conn: &mut Client, scope: &Scope) -> StoreResult<bool> {
    let row = conn
        .query_opt(
            "SELECT 1 FROM episode_entities WHERE tenant_id = $1 AND user_id = $2 AND agent_id = $3 LIMIT 1",
            &[&scope.tenant_id, &scope.user_id, &scope.agent_id],
        )
        .map_err(map_pg_err)?;
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

fn map_pg_err(err: postgres::Error) -> StoreError {
    StoreError::Storage(err.to_string())
}

fn normalize_postgres_dsn(dsn: &str) -> StoreResult<(String, String)> {
    let (base_no_db, db_name, query) = split_postgres_dsn(dsn);
    let db_name = db_name.unwrap_or_else(|| "memweft".to_string());
    let mut normalized = base_no_db;
    normalized.push_str(&db_name);
    if let Some(query) = query {
        normalized.push('?');
        normalized.push_str(&query);
    }
    Ok((normalized, db_name))
}

fn split_postgres_dsn(dsn: &str) -> (String, Option<String>, Option<String>) {
    let (base, query) = match dsn.split_once('?') {
        Some((base, query)) => (base, Some(query.to_string())),
        None => (dsn, None),
    };
    let scheme_end = base.find("://").map(|idx| idx + 3).unwrap_or(0);
    let path_start = base[scheme_end..]
        .find('/')
        .map(|idx| scheme_end + idx);
    match path_start {
        Some(idx) => {
            let db_name = if idx + 1 < base.len() {
                Some(base[idx + 1..].to_string())
            } else {
                None
            };
            let base_no_db = base[..=idx].to_string();
            (base_no_db, db_name, query)
        }
        None => (format!("{}/", base), None, query),
    }
}

fn ensure_postgres_database(dsn: &str, db_name: &str) -> StoreResult<()> {
    let (base_no_db, _, query) = split_postgres_dsn(dsn);
    let mut admin_dsn = base_no_db;
    admin_dsn.push_str("postgres");
    if let Some(query) = query {
        admin_dsn.push('?');
        admin_dsn.push_str(&query);
    }
    let mut admin = Client::connect(&admin_dsn, NoTls).map_err(map_pg_err)?;
    let exists = admin
        .query("SELECT 1 FROM pg_database WHERE datname = $1", &[&db_name])
        .map_err(map_pg_err)?;
    if exists.is_empty() {
        let statement = format!("CREATE DATABASE {}", quote_pg_identifier(db_name));
        admin.execute(statement.as_str(), &[]).map_err(map_pg_err)?;
    }
    Ok(())
}

fn quote_pg_identifier(value: &str) -> String {
    let mut quoted = String::from("\"");
    for ch in value.chars() {
        if ch == '"' {
            quoted.push_str("\"\"");
        } else {
            quoted.push(ch);
        }
    }
    quoted.push('"');
    quoted
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
    fn postgres_store_roundtrip() {
        let dsn = match std::env::var("MEMWEFT_POSTGRES_DSN") {
            Ok(value) if !value.trim().is_empty() => value,
            _ => {
                eprintln!("MEMWEFT_POSTGRES_DSN not set; skipping postgres_store_roundtrip");
                return;
            }
        };

        let store = PostgresStore::new(&dsn).unwrap();
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

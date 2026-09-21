use std::collections::hash_map::DefaultHasher;
use std::env;
use std::fs;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::sync::Once;
use std::time::Duration;

use chrono::{Duration as ChronoDuration, Utc};
use criterion::measurement::WallTime;
use criterion::{criterion_group, criterion_main, BenchmarkGroup, BenchmarkId, Criterion};
use memweft_store::{
    build_memory_packet, BuildRequest, EpisodeFilter, Event, EventKind, FactFilter, InMemoryStore,
    InsightFilter, RecallCues, RecallPolicy, SqliteStore, Store, StmState, TimeRangeFilter,
    WorkingStatePatch,
};
#[cfg(feature = "mysql")]
use memweft_store::MySqlStore;
#[cfg(feature = "postgres")]
use memweft_store::PostgresStore;
#[cfg(feature = "mysql")]
use mysql::prelude::Queryable;
#[cfg(feature = "mysql")]
use mysql::{Opts as MySqlOpts, OptsBuilder as MySqlOptsBuilder, Pool as MySqlPool};
#[cfg(feature = "postgres")]
use postgres::{Client as PostgresClient, NoTls as PostgresNoTls};
use memweft_types::{
    CompressionLevel, Episode, Fact, FactStatus, InsightItem, InsightTrigger, InsightType, JsonMap,
    Procedure, Scope, ScopeLevel, TimeRange, Validity,
};
use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};

const TENANT_ID: &str = "default";
const USER_ID: &str = "bench-user";
const AGENT_ID: &str = "bench-agent";
const SESSION_ID: &str = "bench-session";
const RUN_ID: &str = "bench-run";
const MAX_IN_MEMORY_EVENTS: usize = 300_000;
const MAX_SQLITE_EVENTS: usize = 1_000_000;
#[cfg(feature = "mysql")]
const MAX_MYSQL_EVENTS: usize = 50_000;
#[cfg(feature = "postgres")]
const MAX_POSTGRES_EVENTS: usize = 50_000;
const SQLITE_EVENT_CHUNK: usize = 10_000;
static BENCH_ENV_LOADED: Once = Once::new();
#[cfg(feature = "mysql")]
const MYSQL_RESET_TABLES: [&str; 12] = [
    "events",
    "event_tags",
    "event_entities",
    "wm_state",
    "stm_state",
    "facts",
    "episodes",
    "episode_tags",
    "episode_entities",
    "procedures",
    "insights",
    "context_builds",
];
#[cfg(feature = "postgres")]
const POSTGRES_RESET_TABLES: [&str; 12] = [
    "events",
    "event_tags",
    "event_entities",
    "wm_state",
    "stm_state",
    "facts",
    "episodes",
    "episode_tags",
    "episode_entities",
    "procedures",
    "insights",
    "context_builds",
];

#[derive(Clone, Copy, Debug)]
struct DatasetSize {
    events: usize,
    facts: usize,
    episodes: usize,
    procedures: usize,
    insights: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SqliteMode {
    Memory,
    File,
    Auto,
}

impl DatasetSize {
    fn label(&self) -> String {
        format!(
            "events{}_facts{}_episodes{}_procedures{}_insights{}",
            self.events, self.facts, self.episodes, self.procedures, self.insights
        )
    }
}

fn scope_for_size(size: DatasetSize) -> Scope {
    let suffix = unique_suffix(&size.label());
    Scope {
        tenant_id: format!("{}-{}", TENANT_ID, suffix),
        user_id: format!("{}-{}", USER_ID, suffix),
        agent_id: format!("{}-{}", AGENT_ID, suffix),
        session_id: format!("{}-{}", SESSION_ID, suffix),
        run_id: format!("{}-{}", RUN_ID, suffix),
    }
}

fn main_request(scope: Scope) -> BuildRequest {
    let mut request = BuildRequest::new(scope, memweft_types::Purpose::Planner);
    request.task_type = Some("summary".to_string());
    request.cues = RecallCues {
        tags: vec!["alpha".to_string()],
        entities: vec!["entity1".to_string()],
        keywords: vec!["memweft".to_string()],
        time_range: None,
    };
    request.policy = RecallPolicy {
        max_total_candidates: 100,
        max_facts: 30,
        max_procedures: 5,
        max_episodes: 20,
        max_insights: 10,
        max_key_quotes: 10,
        conversation_window: 5,
        episode_time_window_days: 30,
        last_tool_evidence_limit: 3,
        include_conversation_window: false,
        include_insights_in_tool: false,
        allow_insights_in_responder: false,
    };
    request.persist = false;
    request
}

fn seed_store_common<S: Store + ?Sized>(store: &S, size: DatasetSize, scope: &Scope) {
    let mut rng = StdRng::seed_from_u64(42);
    let now = Utc::now();

    store
        .patch_working_state(
            scope,
            WorkingStatePatch {
                goal: Some("benchmark".to_string()),
                plan: Some(vec!["step1".to_string(), "step2".to_string()]),
                ..WorkingStatePatch::default()
            },
        )
        .unwrap();

    store
        .update_stm(
            scope,
            StmState {
                rolling_summary: "rolling summary".to_string(),
                key_quotes: vec![],
            },
        )
        .unwrap();

    for idx in 0..size.facts {
        let fact = Fact {
            fact_id: format!("f{}", idx),
            fact_key: format!("pref.key.{}", idx),
            value: json!({ "value": idx }),
            status: FactStatus::Active,
            validity: Validity::default(),
            confidence: 0.5 + (idx as f64 / size.facts.max(1) as f64) * 0.5,
            sources: vec!["e0".to_string()],
            scope_level: ScopeLevel::User,
            notes: String::new(),
        };
        store.upsert_fact(scope, fact).unwrap();
    }

    for idx in 0..size.episodes {
        let episode = Episode {
            episode_id: format!("ep{}", idx),
            time_range: TimeRange {
                start: now - ChronoDuration::days(idx as i64 % 30),
                end: None,
            },
            summary: format!("episode {}", idx),
            highlights: vec![format!("highlight {}", idx)],
            tags: vec![if idx % 2 == 0 { "alpha" } else { "beta" }.to_string()],
            entities: vec!["entity1".to_string()],
            sources: vec!["e0".to_string()],
            compression_level: CompressionLevel::Raw,
            recency_score: None,
        };
        store.append_episode(scope, episode).unwrap();
    }

    for idx in 0..size.procedures {
        let procedure = Procedure {
            procedure_id: format!("p{}", idx),
            task_type: "summary".to_string(),
            content: json!({ "step": idx }),
            priority: (size.procedures as i32) - idx as i32,
            sources: vec![],
            applicability: JsonMap::new(),
        };
        store.upsert_procedure(scope, procedure).unwrap();
    }

    for idx in 0..size.insights {
        let insight = InsightItem {
            id: format!("i{}", idx),
            kind: if idx % 2 == 0 {
                InsightType::Hypothesis
            } else {
                InsightType::Pattern
            },
            statement: format!("insight {}", idx),
            trigger: if idx % 2 == 0 {
                InsightTrigger::Synthesis
            } else {
                InsightTrigger::Analogy
            },
            confidence: rng.gen_range(0.1..0.9),
            validation_state: memweft_types::ValidationState::Testing,
            tests_suggested: vec![],
            expires_at: "run_end".to_string(),
            sources: vec![],
        };
        store.append_insight(scope, insight).unwrap();
    }
}

fn seed_events_in_memory(store: &InMemoryStore, size: DatasetSize, scope: &Scope, prefix: &str) {
    let now = Utc::now();
    for idx in 0..size.events {
        let event = Event {
            event_id: format!("{}-e{}", prefix, idx),
            scope: scope.clone(),
            ts: now - ChronoDuration::seconds(idx as i64),
            kind: EventKind::Message,
            payload: json!({ "role": "user", "content": format!("message {}", idx) }),
            tags: vec![if idx % 2 == 0 { "alpha" } else { "beta" }.to_string()],
            entities: vec!["entity1".to_string()],
        };
        store.append_event(event).unwrap();
    }
}

fn seed_events_sqlite(store: &SqliteStore, size: DatasetSize, scope: &Scope, prefix: &str) {
    let now = Utc::now();
    let chunk_size = sqlite_event_chunk();
    let mut buffer = Vec::with_capacity(chunk_size);
    for idx in 0..size.events {
        buffer.push(Event {
            event_id: format!("{}-e{}", prefix, idx),
            scope: scope.clone(),
            ts: now - ChronoDuration::seconds(idx as i64),
            kind: EventKind::Message,
            payload: json!({ "role": "user", "content": format!("message {}", idx) }),
            tags: vec![if idx % 2 == 0 { "alpha" } else { "beta" }.to_string()],
            entities: vec!["entity1".to_string()],
        });
        if buffer.len() >= chunk_size {
            store.append_events_bulk(&buffer).unwrap();
            buffer.clear();
        }
    }
    if !buffer.is_empty() {
        store.append_events_bulk(&buffer).unwrap();
    }
}

#[cfg(any(feature = "mysql", feature = "postgres"))]
fn seed_events_store<S: Store + ?Sized>(
    store: &S,
    size: DatasetSize,
    scope: &Scope,
    prefix: &str,
) {
    let now = Utc::now();
    for idx in 0..size.events {
        let event = Event {
            event_id: format!("{}-e{}", prefix, idx),
            scope: scope.clone(),
            ts: now - ChronoDuration::seconds(idx as i64),
            kind: EventKind::Message,
            payload: json!({ "role": "user", "content": format!("message {}", idx) }),
            tags: vec![if idx % 2 == 0 { "alpha" } else { "beta" }.to_string()],
            entities: vec!["entity1".to_string()],
        };
        store.append_event(event).unwrap();
    }
}

fn bench_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    if size.events > max_in_memory_events() {
        return;
    }
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_in_memory(&store, size, &scope, &event_prefix);
    let request = main_request(scope.clone());

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = build_memory_packet(&store, request.clone()).unwrap();
            })
        },
    );
}

fn bench_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("build_memory_packet_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_sqlite(&store, size, &scope, &event_prefix);
    let request = main_request(scope.clone());

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = build_memory_packet(&store, request.clone()).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    if size.events > max_mysql_events() {
        return;
    }
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let request = main_request(scope.clone());

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = build_memory_packet(&store, request.clone()).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    if size.events > max_postgres_events() {
        return;
    }
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let request = main_request(scope.clone());

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = build_memory_packet(&store, request.clone()).unwrap();
            })
        },
    );
}

fn build_memory_packet_bench(c: &mut Criterion) {
    load_bench_env();
    let mut group = c.benchmark_group("build_memory_packet_events_scale");
    group.sample_size(25);
    group.warm_up_time(Duration::from_secs(3));
    group.measurement_time(Duration::from_secs(6));

    let mut event_scale_sizes = vec![
        DatasetSize {
            events: 200,
            facts: 50,
            episodes: 40,
            procedures: 10,
            insights: 20,
        },
        DatasetSize {
            events: 2000,
            facts: 200,
            episodes: 200,
            procedures: 30,
            insights: 50,
        },
        DatasetSize {
            events: 10000,
            facts: 500,
            episodes: 1000,
            procedures: 50,
            insights: 100,
        },
        DatasetSize {
            events: 100000,
            facts: 500,
            episodes: 1000,
            procedures: 50,
            insights: 100,
        },
        DatasetSize {
            events: 300000,
            facts: 500,
            episodes: 1000,
            procedures: 50,
            insights: 100,
        },
    ];

    if is_extended() {
        event_scale_sizes.push(DatasetSize {
            events: 1_000_000,
            facts: 500,
            episodes: 1000,
            procedures: 50,
            insights: 100,
        });
    }

    if is_extreme() {
        event_scale_sizes.push(DatasetSize {
            events: 5_000_000,
            facts: 500,
            episodes: 1000,
            procedures: 50,
            insights: 100,
        });
    }

    for events in parse_usize_list_env("MEMWEFT_BENCH_EVENTS_SCALES") {
        if event_scale_sizes.iter().any(|size| size.events == events) {
            continue;
        }
        event_scale_sizes.push(DatasetSize {
            events,
            facts: 500,
            episodes: 1000,
            procedures: 50,
            insights: 100,
        });
    }
    event_scale_sizes.sort_by_key(|size| size.events);

    for size in event_scale_sizes {
        bench_in_memory(&mut group, size);
        bench_sqlite(&mut group, size);
        #[cfg(feature = "mysql")]
        bench_mysql(&mut group, size);
        #[cfg(feature = "postgres")]
        bench_postgres(&mut group, size);
    }
    group.finish();

    let mut candidate_group = c.benchmark_group("build_memory_packet_candidate_scale");
    candidate_group.sample_size(25);
    candidate_group.warm_up_time(Duration::from_secs(3));
    candidate_group.measurement_time(Duration::from_secs(6));

    let mut candidate_scale_sizes = vec![
        DatasetSize {
            events: 2000,
            facts: 50,
            episodes: 40,
            procedures: 10,
            insights: 20,
        },
        DatasetSize {
            events: 2000,
            facts: 200,
            episodes: 200,
            procedures: 30,
            insights: 50,
        },
        DatasetSize {
            events: 2000,
            facts: 1000,
            episodes: 1000,
            procedures: 80,
            insights: 200,
        },
        DatasetSize {
            events: 2000,
            facts: 2000,
            episodes: 2000,
            procedures: 120,
            insights: 300,
        },
    ];

    if is_extended() {
        candidate_scale_sizes.push(DatasetSize {
            events: 2000,
            facts: 5000,
            episodes: 5000,
            procedures: 200,
            insights: 500,
        });
    }

    if is_extreme() {
        candidate_scale_sizes.push(DatasetSize {
            events: 2000,
            facts: 10_000,
            episodes: 10_000,
            procedures: 300,
            insights: 800,
        });
    }

    for size in candidate_scale_sizes {
        bench_in_memory(&mut candidate_group, size);
        bench_sqlite(&mut candidate_group, size);
        #[cfg(feature = "mysql")]
        bench_mysql(&mut candidate_group, size);
        #[cfg(feature = "postgres")]
        bench_postgres(&mut candidate_group, size);
    }
    candidate_group.finish();
}

fn store_ops_bench(c: &mut Criterion) {
    load_bench_env();
    let mut events_group = c.benchmark_group("store_ops_list_events");
    events_group.sample_size(20);
    events_group.warm_up_time(Duration::from_secs(2));
    events_group.measurement_time(Duration::from_secs(5));
    for size in list_events_sizes() {
        bench_list_events_in_memory(&mut events_group, size);
        bench_list_events_sqlite(&mut events_group, size);
        #[cfg(feature = "mysql")]
        bench_list_events_mysql(&mut events_group, size);
        #[cfg(feature = "postgres")]
        bench_list_events_postgres(&mut events_group, size);
    }
    events_group.finish();

    let mut facts_group = c.benchmark_group("store_ops_list_facts");
    facts_group.sample_size(20);
    facts_group.warm_up_time(Duration::from_secs(2));
    facts_group.measurement_time(Duration::from_secs(5));
    for size in list_facts_sizes() {
        bench_list_facts_in_memory(&mut facts_group, size);
        bench_list_facts_sqlite(&mut facts_group, size);
        #[cfg(feature = "mysql")]
        bench_list_facts_mysql(&mut facts_group, size);
        #[cfg(feature = "postgres")]
        bench_list_facts_postgres(&mut facts_group, size);
    }
    facts_group.finish();

    let mut episodes_group = c.benchmark_group("store_ops_list_episodes");
    episodes_group.sample_size(20);
    episodes_group.warm_up_time(Duration::from_secs(2));
    episodes_group.measurement_time(Duration::from_secs(5));
    for size in list_episodes_sizes() {
        bench_list_episodes_in_memory(&mut episodes_group, size);
        bench_list_episodes_sqlite(&mut episodes_group, size);
        #[cfg(feature = "mysql")]
        bench_list_episodes_mysql(&mut episodes_group, size);
        #[cfg(feature = "postgres")]
        bench_list_episodes_postgres(&mut episodes_group, size);
    }
    episodes_group.finish();

    let mut insights_group = c.benchmark_group("store_ops_list_insights");
    insights_group.sample_size(20);
    insights_group.warm_up_time(Duration::from_secs(2));
    insights_group.measurement_time(Duration::from_secs(5));
    for size in list_insights_sizes() {
        bench_list_insights_in_memory(&mut insights_group, size);
        bench_list_insights_sqlite(&mut insights_group, size);
        #[cfg(feature = "mysql")]
        bench_list_insights_mysql(&mut insights_group, size);
        #[cfg(feature = "postgres")]
        bench_list_insights_postgres(&mut insights_group, size);
    }
    insights_group.finish();

    let mut procedures_group = c.benchmark_group("store_ops_list_procedures");
    procedures_group.sample_size(20);
    procedures_group.warm_up_time(Duration::from_secs(2));
    procedures_group.measurement_time(Duration::from_secs(5));
    for size in list_procedures_sizes() {
        bench_list_procedures_in_memory(&mut procedures_group, size);
        bench_list_procedures_sqlite(&mut procedures_group, size);
        #[cfg(feature = "mysql")]
        bench_list_procedures_mysql(&mut procedures_group, size);
        #[cfg(feature = "postgres")]
        bench_list_procedures_postgres(&mut procedures_group, size);
    }
    procedures_group.finish();

    let mut append_group = c.benchmark_group("store_ops_append_event");
    append_group.sample_size(20);
    append_group.warm_up_time(Duration::from_secs(2));
    append_group.measurement_time(Duration::from_secs(5));
    for size in write_event_sizes() {
        bench_append_event_in_memory(&mut append_group, size);
        bench_append_event_sqlite(&mut append_group, size);
        #[cfg(feature = "mysql")]
        bench_append_event_mysql(&mut append_group, size);
        #[cfg(feature = "postgres")]
        bench_append_event_postgres(&mut append_group, size);
    }
    append_group.finish();

    let mut bulk_group = c.benchmark_group("store_ops_append_events_bulk");
    bulk_group.sample_size(15);
    bulk_group.warm_up_time(Duration::from_secs(2));
    bulk_group.measurement_time(Duration::from_secs(5));
    for size in write_event_bulk_sizes() {
        bench_append_events_bulk_sqlite(&mut bulk_group, size);
        #[cfg(feature = "mysql")]
        bench_append_events_bulk_mysql(&mut bulk_group, size);
        #[cfg(feature = "postgres")]
        bench_append_events_bulk_postgres(&mut bulk_group, size);
    }
    bulk_group.finish();

    let mut upsert_group = c.benchmark_group("store_ops_upsert_fact");
    upsert_group.sample_size(20);
    upsert_group.warm_up_time(Duration::from_secs(2));
    upsert_group.measurement_time(Duration::from_secs(5));
    for size in write_fact_sizes() {
        bench_upsert_fact_in_memory(&mut upsert_group, size);
        bench_upsert_fact_sqlite(&mut upsert_group, size);
        #[cfg(feature = "mysql")]
        bench_upsert_fact_mysql(&mut upsert_group, size);
        #[cfg(feature = "postgres")]
        bench_upsert_fact_postgres(&mut upsert_group, size);
    }
    upsert_group.finish();
}

fn list_events_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 2000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 10000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 100000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 300000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 1_000_000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    sizes
}

fn list_facts_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 50,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 200,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 1000,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 5000,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 10000,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    sizes
}

fn list_episodes_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 40,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 200,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 1000,
            procedures: 0,
            insights: 0,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 0,
            episodes: 5000,
            procedures: 0,
            insights: 0,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 0,
            episodes: 10000,
            procedures: 0,
            insights: 0,
        });
    }

    sizes
}

fn list_insights_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 20,
        },
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 50,
        },
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 200,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 500,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 1000,
        });
    }

    sizes
}

fn list_procedures_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 10,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 30,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 80,
            insights: 0,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 200,
            insights: 0,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 300,
            insights: 0,
        });
    }

    sizes
}

fn write_event_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 10_000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 100_000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 300_000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 1_000_000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    for events in parse_usize_list_env("MEMWEFT_BENCH_WRITE_EVENTS_SCALES") {
        if sizes.iter().any(|size| size.events == events) {
            continue;
        }
        sizes.push(DatasetSize {
            events,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }
    sizes.sort_by_key(|size| size.events);
    sizes
}

fn write_event_bulk_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 10_000,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
    ];
    for events in parse_usize_list_env("MEMWEFT_BENCH_BULK_EVENTS_SCALES") {
        if sizes.iter().any(|size| size.events == events) {
            continue;
        }
        sizes.push(DatasetSize {
            events,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }
    sizes.sort_by_key(|size| size.events);
    sizes
}

fn write_fact_sizes() -> Vec<DatasetSize> {
    let mut sizes = vec![
        DatasetSize {
            events: 0,
            facts: 0,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 200,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
        DatasetSize {
            events: 0,
            facts: 1000,
            episodes: 0,
            procedures: 0,
            insights: 0,
        },
    ];

    if is_extended() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 5000,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    if is_extreme() {
        sizes.push(DatasetSize {
            events: 0,
            facts: 10000,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }

    for facts in parse_usize_list_env("MEMWEFT_BENCH_WRITE_FACT_SCALES") {
        if sizes.iter().any(|size| size.facts == facts) {
            continue;
        }
        sizes.push(DatasetSize {
            events: 0,
            facts,
            episodes: 0,
            procedures: 0,
            insights: 0,
        });
    }
    sizes.sort_by_key(|size| size.facts);
    sizes
}

fn bench_list_events_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    if size.events > max_in_memory_events() {
        return;
    }
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_in_memory(&store, size, &scope, &event_prefix);
    let range = TimeRangeFilter::default();
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_events(&scope, range.clone(), limit).unwrap();
            })
        },
    );
}

fn bench_list_events_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("list_events_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_sqlite(&store, size, &scope, &event_prefix);
    let range = TimeRangeFilter::default();
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_events(&scope, range.clone(), limit).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_list_events_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    if size.events > max_mysql_events() {
        return;
    }
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let range = TimeRangeFilter::default();
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_events(&scope, range.clone(), limit).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_list_events_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    if size.events > max_postgres_events() {
        return;
    }
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let range = TimeRangeFilter::default();
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_events(&scope, range.clone(), limit).unwrap();
            })
        },
    );
}

fn bench_list_facts_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = FactFilter {
        limit: Some(200),
        ..FactFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_facts(&scope, filter.clone()).unwrap();
            })
        },
    );
}

fn bench_list_facts_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("list_facts_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = FactFilter {
        limit: Some(200),
        ..FactFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_facts(&scope, filter.clone()).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_list_facts_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = FactFilter {
        limit: Some(200),
        ..FactFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_facts(&scope, filter.clone()).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_list_facts_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = FactFilter {
        limit: Some(200),
        ..FactFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_facts(&scope, filter.clone()).unwrap();
            })
        },
    );
}

fn bench_list_episodes_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = EpisodeFilter {
        limit: Some(200),
        ..EpisodeFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_episodes(&scope, filter.clone()).unwrap();
            })
        },
    );
}

fn bench_list_episodes_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("list_episodes_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = EpisodeFilter {
        limit: Some(200),
        ..EpisodeFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_episodes(&scope, filter.clone()).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_list_episodes_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = EpisodeFilter {
        limit: Some(200),
        ..EpisodeFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_episodes(&scope, filter.clone()).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_list_episodes_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = EpisodeFilter {
        limit: Some(200),
        ..EpisodeFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_episodes(&scope, filter.clone()).unwrap();
            })
        },
    );
}

fn bench_list_insights_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = InsightFilter {
        limit: Some(200),
        ..InsightFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_insights(&scope, filter.clone()).unwrap();
            })
        },
    );
}

fn bench_list_insights_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("list_insights_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = InsightFilter {
        limit: Some(200),
        ..InsightFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_insights(&scope, filter.clone()).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_list_insights_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = InsightFilter {
        limit: Some(200),
        ..InsightFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_insights(&scope, filter.clone()).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_list_insights_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let filter = InsightFilter {
        limit: Some(200),
        ..InsightFilter::default()
    };

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_insights(&scope, filter.clone()).unwrap();
            })
        },
    );
}

fn bench_list_procedures_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_procedures(&scope, "summary", limit).unwrap();
            })
        },
    );
}

fn bench_list_procedures_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("list_procedures_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_procedures(&scope, "summary", limit).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_list_procedures_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_procedures(&scope, "summary", limit).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_list_procedures_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let limit = Some(200);

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let _ = store.list_procedures(&scope, "summary", limit).unwrap();
            })
        },
    );
}

fn build_write_event(scope: &Scope, event_id: String, ts: chrono::DateTime<Utc>) -> Event {
    Event {
        event_id,
        scope: scope.clone(),
        ts,
        kind: EventKind::Message,
        payload: json!({ "role": "user", "content": "bench write" }),
        tags: vec![],
        entities: vec![],
    }
}

fn bench_append_event_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    if size.events > max_in_memory_events() {
        return;
    }
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_in_memory(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let event_id = format!("{}-w{}", event_prefix, counter);
                let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                let event = build_write_event(&scope, event_id, ts);
                store.append_event(event).unwrap();
            })
        },
    );
}

fn bench_append_event_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("append_event_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_sqlite(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let event_id = format!("{}-w{}", event_prefix, counter);
                let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                let event = build_write_event(&scope, event_id, ts);
                store.append_event(event).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_append_event_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    if size.events > max_mysql_events() {
        return;
    }
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let event_id = format!("{}-w{}", event_prefix, counter);
                let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                let event = build_write_event(&scope, event_id, ts);
                store.append_event(event).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_append_event_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    if size.events > max_postgres_events() {
        return;
    }
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let event_id = format!("{}-w{}", event_prefix, counter);
                let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                let event = build_write_event(&scope, event_id, ts);
                store.append_event(event).unwrap();
            })
        },
    );
}

fn bench_append_events_bulk_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("append_events_bulk_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_sqlite(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();
    let batch_size = bulk_event_batch_size();

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let mut batch = Vec::with_capacity(batch_size);
                for _ in 0..batch_size {
                    counter += 1;
                    let event_id = format!("{}-w{}", event_prefix, counter);
                    let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                    batch.push(build_write_event(&scope, event_id, ts));
                }
                store.append_events_bulk(&batch).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_append_events_bulk_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    if size.events > max_mysql_events() {
        return;
    }
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();
    let batch_size = bulk_event_batch_size();

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let mut batch = Vec::with_capacity(batch_size);
                for _ in 0..batch_size {
                    counter += 1;
                    let event_id = format!("{}-w{}", event_prefix, counter);
                    let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                    batch.push(build_write_event(&scope, event_id, ts));
                }
                store.append_events_bulk(&batch).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_append_events_bulk_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    if size.events > max_postgres_events() {
        return;
    }
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let event_prefix = event_id_prefix(&scope);
    seed_events_store(&store, size, &scope, &event_prefix);
    let mut counter = size.events;
    let base_ts = Utc::now();
    let batch_size = bulk_event_batch_size();

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                let mut batch = Vec::with_capacity(batch_size);
                for _ in 0..batch_size {
                    counter += 1;
                    let event_id = format!("{}-w{}", event_prefix, counter);
                    let ts = base_ts + ChronoDuration::milliseconds(counter as i64);
                    batch.push(build_write_event(&scope, event_id, ts));
                }
                store.append_events_bulk(&batch).unwrap();
            })
        },
    );
}

fn bench_upsert_fact_in_memory(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let store = InMemoryStore::new();
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let mut counter = size.facts;

    group.bench_with_input(
        BenchmarkId::new("memory", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let fact = Fact {
                    fact_id: format!("f{}", counter),
                    fact_key: format!("bench.fact.{}", counter),
                    value: json!({ "value": counter }),
                    status: FactStatus::Active,
                    validity: Validity::default(),
                    confidence: 0.75,
                    sources: vec![],
                    scope_level: ScopeLevel::User,
                    notes: String::new(),
                };
                store.upsert_fact(&scope, fact).unwrap();
            })
        },
    );
}

fn bench_upsert_fact_sqlite(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some((store, cleanup)) =
        sqlite_store_for_size(size, &format!("upsert_fact_sqlite_{}", size.label()))
    else {
        return;
    };
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let mut counter = size.facts;

    group.bench_with_input(
        BenchmarkId::new("sqlite", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let fact = Fact {
                    fact_id: format!("f{}", counter),
                    fact_key: format!("bench.fact.{}", counter),
                    value: json!({ "value": counter }),
                    status: FactStatus::Active,
                    validity: Validity::default(),
                    confidence: 0.75,
                    sources: vec![],
                    scope_level: ScopeLevel::User,
                    notes: String::new(),
                };
                store.upsert_fact(&scope, fact).unwrap();
            })
        },
    );
    drop(store);
    cleanup_sqlite_file(cleanup);
}

#[cfg(feature = "mysql")]
fn bench_upsert_fact_mysql(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = mysql_dsn() else {
        return;
    };
    let store = MySqlStore::new(&dsn).unwrap();
    reset_mysql_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let mut counter = size.facts;

    group.bench_with_input(
        BenchmarkId::new("mysql", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let fact = Fact {
                    fact_id: format!("f{}", counter),
                    fact_key: format!("bench.fact.{}", counter),
                    value: json!({ "value": counter }),
                    status: FactStatus::Active,
                    validity: Validity::default(),
                    confidence: 0.75,
                    sources: vec![],
                    scope_level: ScopeLevel::User,
                    notes: String::new(),
                };
                store.upsert_fact(&scope, fact).unwrap();
            })
        },
    );
}

#[cfg(feature = "postgres")]
fn bench_upsert_fact_postgres(group: &mut BenchmarkGroup<'_, WallTime>, size: DatasetSize) {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    let store = PostgresStore::new(&dsn).unwrap();
    reset_postgres_tables(&dsn);
    let scope = scope_for_size(size);
    seed_store_common(&store, size, &scope);
    let mut counter = size.facts;

    group.bench_with_input(
        BenchmarkId::new("postgres", size.label()),
        &size,
        |b, _| {
            b.iter(|| {
                counter += 1;
                let fact = Fact {
                    fact_id: format!("f{}", counter),
                    fact_key: format!("bench.fact.{}", counter),
                    value: json!({ "value": counter }),
                    status: FactStatus::Active,
                    validity: Validity::default(),
                    confidence: 0.75,
                    sources: vec![],
                    scope_level: ScopeLevel::User,
                    notes: String::new(),
                };
                store.upsert_fact(&scope, fact).unwrap();
            })
        },
    );
}

fn load_bench_env() {
    BENCH_ENV_LOADED.call_once(|| {
        let Some(path) = bench_config_path() else {
            return;
        };
        let Ok(contents) = fs::read_to_string(&path) else {
            return;
        };
        for raw_line in contents.lines() {
            let line = raw_line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            let key = key.trim();
            if key.is_empty() || env::var(key).is_ok() {
                continue;
            }
            let mut value = value.trim().to_string();
            if (value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\''))
            {
                value = value[1..value.len().saturating_sub(1)].to_string();
            }
            if !value.is_empty() {
                unsafe {
                    env::set_var(key, value);
                }
            }
        }
    });
}

fn bench_config_path() -> Option<PathBuf> {
    if let Ok(value) = env::var("MEMWEFT_BENCH_CONFIG") {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    find_default_bench_config()
}

fn find_default_bench_config() -> Option<PathBuf> {
    let mut dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    loop {
        let candidate = dir.join("bench/memweft_bench.env");
        if candidate.exists() {
            return Some(candidate);
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

fn parse_usize_list_env(key: &str) -> Vec<usize> {
    match env::var(key) {
        Ok(value) => parse_usize_list(&value),
        Err(_) => Vec::new(),
    }
}

fn parse_usize_list(value: &str) -> Vec<usize> {
    value
        .split(',')
        .filter_map(|raw| {
            let cleaned = raw.trim().replace('_', "");
            if cleaned.is_empty() {
                return None;
            }
            cleaned.parse::<usize>().ok()
        })
        .collect()
}

fn is_extended() -> bool {
    matches!(
        env::var("MEMWEFT_BENCH_EXTENDED").as_deref(),
        Ok("1") | Ok("true") | Ok("TRUE")
    )
}

fn is_extreme() -> bool {
    matches!(
        env::var("MEMWEFT_BENCH_EXTREME").as_deref(),
        Ok("1") | Ok("true") | Ok("TRUE")
    )
}

fn max_in_memory_events() -> usize {
    if let Ok(value) = env::var("MEMWEFT_BENCH_INMEMORY_MAX_EVENTS") {
        if let Ok(parsed) = value.parse::<usize>() {
            return parsed;
        }
    }
    MAX_IN_MEMORY_EVENTS
}

fn max_sqlite_events() -> usize {
    if let Ok(value) = env::var("MEMWEFT_BENCH_SQLITE_MAX_EVENTS") {
        if let Ok(parsed) = value.parse::<usize>() {
            return parsed;
        }
    }
    MAX_SQLITE_EVENTS
}

fn bench_reset_db() -> bool {
    matches!(
        env::var("MEMWEFT_BENCH_RESET_DB").as_deref(),
        Ok("1") | Ok("true") | Ok("TRUE")
    )
}

fn sqlite_mode() -> SqliteMode {
    let Ok(value) = env::var("MEMWEFT_BENCH_SQLITE_MODE") else {
        return SqliteMode::Memory;
    };
    match value.trim().to_ascii_lowercase().as_str() {
        "file" => SqliteMode::File,
        "auto" => SqliteMode::Auto,
        _ => SqliteMode::Memory,
    }
}

fn sqlite_store_for_size(size: DatasetSize, label: &str) -> Option<(SqliteStore, Option<PathBuf>)> {
    let use_file = match sqlite_mode() {
        SqliteMode::Memory => false,
        SqliteMode::File => true,
        SqliteMode::Auto => size.events > max_sqlite_events(),
    };

    if !use_file && size.events > max_sqlite_events() {
        return None;
    }

    if use_file {
        let (path, keep_file) = sqlite_file_path(label);
        if bench_reset_db() {
            let _ = fs::remove_file(&path);
        }
        let store = SqliteStore::new(path.clone()).unwrap();
        let cleanup = if keep_file { None } else { Some(path) };
        Some((store, cleanup))
    } else {
        Some((SqliteStore::new_in_memory().unwrap(), None))
    }
}

fn sqlite_file_path(label: &str) -> (PathBuf, bool) {
    if let Ok(value) = env::var("MEMWEFT_BENCH_SQLITE_FILE") {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return (PathBuf::from(trimmed), true);
        }
    }

    let dir = env::var("MEMWEFT_BENCH_SQLITE_DIR").unwrap_or_else(|_| "target/bench_sqlite".into());
    let filename = format!("memweft-{}.db", sanitize_filename(&unique_suffix(label)));
    (PathBuf::from(dir).join(filename), false)
}

fn sanitize_filename(value: &str) -> String {
    value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.') {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

fn cleanup_sqlite_file(path: Option<PathBuf>) {
    if let Some(path) = path {
        let _ = fs::remove_file(path);
    }
}

fn sqlite_event_chunk() -> usize {
    if let Ok(value) = env::var("MEMWEFT_BENCH_SQLITE_EVENT_CHUNK") {
        if let Ok(parsed) = value.parse::<usize>() {
            return parsed.max(1);
        }
    }
    SQLITE_EVENT_CHUNK
}

fn bulk_event_batch_size() -> usize {
    if let Ok(value) = env::var("MEMWEFT_BENCH_BULK_EVENT_BATCH") {
        if let Ok(parsed) = value.parse::<usize>() {
            return parsed.max(1);
        }
    }
    500
}

fn unique_suffix(label: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{}-{}", label, nanos)
}

fn event_id_prefix(scope: &Scope) -> String {
    let mut hasher = DefaultHasher::new();
    scope.run_id.hash(&mut hasher);
    format!("{:x}", hasher.finish())
}

#[cfg(feature = "mysql")]
fn reset_mysql_tables(dsn: &str) {
    if !bench_reset_db() {
        return;
    }
    let mut opts = MySqlOpts::from_url(dsn)
        .unwrap_or_else(|err| panic!("invalid mysql dsn: {}", err));
    if opts.get_db_name().is_none() {
        opts = MySqlOptsBuilder::from_opts(opts)
            .db_name(Some("memweft".to_string()))
            .into();
    }
    let pool = MySqlPool::new(opts).expect("mysql pool");
    let mut conn = pool.get_conn().expect("mysql conn");
    let _ = conn.exec_drop("SET FOREIGN_KEY_CHECKS = 0", ());
    for table in MYSQL_RESET_TABLES {
        conn.exec_drop(format!("TRUNCATE TABLE {}", table), ())
            .unwrap_or_else(|err| panic!("mysql truncate {table}: {err}"));
    }
    let _ = conn.exec_drop("SET FOREIGN_KEY_CHECKS = 1", ());
}

#[cfg(feature = "postgres")]
fn reset_postgres_tables(dsn: &str) {
    if !bench_reset_db() {
        return;
    }
    let dsn = ensure_database_in_dsn(dsn, "memweft");
    let mut conn = PostgresClient::connect(&dsn, PostgresNoTls)
        .unwrap_or_else(|err| panic!("postgres connect failed: {}", err));
    for table in POSTGRES_RESET_TABLES {
        conn.batch_execute(&format!("TRUNCATE TABLE {} RESTART IDENTITY", table))
            .unwrap_or_else(|err| panic!("postgres truncate {table}: {err}"));
    }
}

#[cfg(feature = "postgres")]
fn ensure_database_in_dsn(dsn: &str, database: &str) -> String {
    if dsn_has_database(dsn) {
        return dsn.to_string();
    }
    let (base, query) = match dsn.split_once('?') {
        Some((base, query)) => (base, Some(query)),
        None => (dsn, None),
    };
    let mut normalized = if base.ends_with('/') {
        format!("{}{}", base, database)
    } else {
        format!("{}/{}", base, database)
    };
    if let Some(query) = query {
        normalized.push('?');
        normalized.push_str(query);
    }
    normalized
}

#[cfg(feature = "postgres")]
fn dsn_has_database(dsn: &str) -> bool {
    let mut iter = dsn.splitn(2, '?');
    let base = iter.next().unwrap_or(dsn);
    let query = iter.next().unwrap_or("");
    if query.contains("dbname=") || query.contains("database=") {
        return true;
    }
    let scheme_end = base.find("://").map(|idx| idx + 3).unwrap_or(0);
    match base[scheme_end..].find('/') {
        Some(idx) => scheme_end + idx + 1 < base.len(),
        None => false,
    }
}

#[cfg(feature = "mysql")]
fn mysql_dsn() -> Option<String> {
    match env::var("MEMWEFT_BENCH_MYSQL_DSN") {
        Ok(value) if !value.trim().is_empty() => Some(value),
        _ => None,
    }
}

#[cfg(feature = "postgres")]
fn postgres_dsn() -> Option<String> {
    match env::var("MEMWEFT_BENCH_POSTGRES_DSN") {
        Ok(value) if !value.trim().is_empty() => Some(value),
        _ => None,
    }
}

#[cfg(feature = "mysql")]
fn max_mysql_events() -> usize {
    if let Ok(value) = env::var("MEMWEFT_BENCH_MYSQL_MAX_EVENTS") {
        if let Ok(parsed) = value.parse::<usize>() {
            return parsed;
        }
    }
    MAX_MYSQL_EVENTS
}

#[cfg(feature = "postgres")]
fn max_postgres_events() -> usize {
    if let Ok(value) = env::var("MEMWEFT_BENCH_POSTGRES_MAX_EVENTS") {
        if let Ok(parsed) = value.parse::<usize>() {
            return parsed;
        }
    }
    MAX_POSTGRES_EVENTS
}

criterion_group!(benches, build_memory_packet_bench, store_ops_bench);
criterion_main!(benches);

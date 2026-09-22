use chrono::{DateTime, Utc};
use memweft_types::{
    EvidenceRef, Fact, FactStatus, InsightItem, JsonMap, KeyQuote, MemoryPacket, Procedure, Scope,
    ValidationState, WorkingState,
};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::RwLock;

mod composer;
mod documents;
mod pools;
mod indexed_recall;
mod lexical;
pub use indexed_recall::RecallCandidates;
pub use lexical::terms as lexical_terms;
pub use documents::{Document, Mutation};
pub use pools::{PoolFact, PoolRef, PoolRevision};
mod sqlite;
mod checkpoint;
#[cfg(feature = "mysql")]
mod mysql;
#[cfg(feature = "postgres")]
mod postgres;

pub use composer::{build_memory_packet, BuildRequest, RecallCues, RecallPolicy};
pub use sqlite::{SqliteStore, SqliteOptions};
#[cfg(feature = "mysql")]
pub use mysql::MySqlStore;
#[cfg(feature = "postgres")]
pub use postgres::PostgresStore;

pub type StoreResult<T> = Result<T, StoreError>;

#[derive(Debug)]
pub enum StoreError {
    NotFound,
    Poisoned,
    InvalidInput(String),
    Storage(String),
    Conflict(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::NotFound => write!(f, "item not found"),
            StoreError::Poisoned => write!(f, "lock poisoned"),
            StoreError::InvalidInput(msg) => write!(f, "invalid input: {}", msg),
            StoreError::Storage(msg) => write!(f, "storage error: {}", msg),
            StoreError::Conflict(msg) => write!(f, "conflict: {}", msg),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(err: rusqlite::Error) -> Self {
        StoreError::Storage(err.to_string())
    }
}

impl From<serde_json::Error> for StoreError {
    fn from(err: serde_json::Error) -> Self {
        StoreError::InvalidInput(err.to_string())
    }
}

#[derive(Debug, Clone)]
pub struct Event {
    pub event_id: String,
    pub scope: Scope,
    pub ts: DateTime<Utc>,
    pub kind: EventKind,
    pub payload: Value,
    pub tags: Vec<String>,
    pub entities: Vec<String>,
}

#[derive(Debug, Clone)]
pub enum EventKind {
    Message,
    ToolResult,
    StatePatch,
    System,
}

#[derive(Debug, Clone, Default)]
pub struct TimeRangeFilter {
    pub start: Option<DateTime<Utc>>,
    pub end: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Default)]
pub struct FactFilter {
    pub status: Option<Vec<FactStatus>>,
    pub valid_at: Option<DateTime<Utc>>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Default)]
pub struct EpisodeFilter {
    pub time_range: Option<TimeRangeFilter>,
    pub tags: Vec<String>,
    pub entities: Vec<String>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Default)]
pub struct InsightFilter {
    pub validation_state: Option<Vec<ValidationState>>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Default)]
pub struct StmState {
    pub rolling_summary: String,
    pub key_quotes: Vec<KeyQuote>,
}

#[derive(Debug, Clone, Default)]
pub struct WorkingStatePatch {
    pub goal: Option<String>,
    pub plan: Option<Vec<String>>,
    pub slots: Option<JsonMap>,
    pub constraints: Option<JsonMap>,
    pub tool_evidence: Option<Vec<EvidenceRef>>,
    pub decisions: Option<Vec<String>>,
    pub risks: Option<Vec<String>>,
    pub state_version: Option<u32>,
}

pub trait Store: Send + Sync {
    /// Instance-level operational diagnostics; never returns memory contents.
    fn storage_status(&self) -> StoreResult<Value> {
        Ok(serde_json::json!({"supported": false}))
    }
    /// Ordered pools already authorized by the caller. None preserves the legacy
    /// fallback on backends without an indexed implementation. Limit includes
    /// any caller-requested diagnostic sample; it is not a global hit count.
    fn recall_candidates(&self, _scope: &Scope, _pools: &[String], _query: Option<&str>, _limit: usize) -> StoreResult<Option<RecallCandidates>> {
        Ok(None)
    }
    fn pool_facts(&self, _scope: &Scope, _pool: &str) -> StoreResult<Vec<PoolFact>> {
        Err(StoreError::InvalidInput("memory pools require the sqlite backend".into()))
    }
    fn put_pool_fact(&self, _scope: &Scope, _pool: &str, _fact: Fact, _expected_revision: Option<u64>) -> StoreResult<PoolFact> {
        Err(StoreError::InvalidInput("memory pools require the sqlite backend".into()))
    }
    fn forget_pool_fact(&self, _scope: &Scope, _pool: &str, _key: &str, _expected_revision: Option<u64>) -> StoreResult<bool> {
        Err(StoreError::InvalidInput("memory pools require the sqlite backend".into()))
    }
    fn mutate_documents_checked(&self, scope: &Scope, mutations: &[Mutation], guards: &[PoolRevision]) -> StoreResult<()> {
        if !guards.is_empty() {
            return Err(StoreError::InvalidInput("pool revision checks require the sqlite backend".into()));
        }
        self.mutate_documents(scope, mutations)
    }
    /// Versioned JSON documents. The initial implementation supports SQLite.
    fn documents(&self, _scope: &Scope, _prefix: &[String]) -> StoreResult<Vec<Document>> {
        Err(StoreError::InvalidInput("document API requires the sqlite backend".into()))
    }
    fn mutate_documents(&self, _scope: &Scope, _mutations: &[Mutation]) -> StoreResult<()> {
        Err(StoreError::InvalidInput("document API requires the sqlite backend".into()))
    }
    fn forget_fact(&self, _scope: &Scope, _key: &str) -> StoreResult<bool> {
        Err(StoreError::InvalidInput("forget requires the sqlite backend".into()))
    }
    fn append_event(&self, event: Event) -> StoreResult<()>;
    fn list_events(
        &self,
        scope: &Scope,
        range: TimeRangeFilter,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Event>>;

    fn get_working_state(&self, scope: &Scope) -> StoreResult<Option<WorkingState>>;
    fn patch_working_state(
        &self,
        scope: &Scope,
        patch: WorkingStatePatch,
    ) -> StoreResult<WorkingState>;

    fn get_stm(&self, scope: &Scope) -> StoreResult<Option<StmState>>;
    fn update_stm(&self, scope: &Scope, stm: StmState) -> StoreResult<()>;

    fn list_facts(&self, scope: &Scope, filter: FactFilter) -> StoreResult<Vec<Fact>>;
    fn upsert_fact(&self, scope: &Scope, fact: Fact) -> StoreResult<()>;

    fn list_episodes(
        &self,
        scope: &Scope,
        filter: EpisodeFilter,
    ) -> StoreResult<Vec<memweft_types::Episode>>;
    fn append_episode(&self, scope: &Scope, episode: memweft_types::Episode) -> StoreResult<()>;

    fn list_procedures(
        &self,
        scope: &Scope,
        task_type: &str,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Procedure>>;
    fn upsert_procedure(&self, scope: &Scope, procedure: Procedure) -> StoreResult<()>;

    fn list_insights(&self, scope: &Scope, filter: InsightFilter) -> StoreResult<Vec<InsightItem>>;
    fn append_insight(&self, scope: &Scope, insight: InsightItem) -> StoreResult<()>;

    fn write_context_build(&self, scope: &Scope, packet: MemoryPacket) -> StoreResult<()>;
    fn list_context_builds(
        &self,
        scope: &Scope,
        limit: Option<usize>,
    ) -> StoreResult<Vec<MemoryPacket>>;
}

#[derive(Debug, Default)]
pub struct InMemoryStore {
    events: RwLock<Vec<Event>>,
    wm_state: RwLock<HashMap<RunKey, WorkingState>>,
    stm_state: RwLock<HashMap<SessionKey, StmState>>,
    facts: RwLock<HashMap<LtmKey, Vec<Fact>>>,
    episodes: RwLock<HashMap<LtmKey, Vec<memweft_types::Episode>>>,
    procedures: RwLock<HashMap<LtmKey, Vec<Procedure>>>,
    insights: RwLock<HashMap<RunKey, Vec<InsightItem>>>,
    context_builds: RwLock<HashMap<RunKey, Vec<MemoryPacket>>>,
}

impl InMemoryStore {
    pub fn new() -> Self {
        Self::default()
    }
}

impl Store for InMemoryStore {
    fn append_event(&self, event: Event) -> StoreResult<()> {
        let mut guard = self.events.write().map_err(|_| StoreError::Poisoned)?;
        guard.push(event);
        Ok(())
    }

    fn list_events(
        &self,
        scope: &Scope,
        range: TimeRangeFilter,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Event>> {
        let guard = self.events.read().map_err(|_| StoreError::Poisoned)?;
        let mut results: Vec<Event> = guard
            .iter()
            .filter(|e| scope_matches(&e.scope, scope))
            .filter(|e| match range.start {
                Some(start) => e.ts >= start,
                None => true,
            })
            .filter(|e| match range.end {
                Some(end) => e.ts <= end,
                None => true,
            })
            .cloned()
            .collect();

        apply_limit(&mut results, limit);
        Ok(results)
    }

    fn get_working_state(&self, scope: &Scope) -> StoreResult<Option<WorkingState>> {
        let key = RunKey::from(scope);
        let guard = self.wm_state.read().map_err(|_| StoreError::Poisoned)?;
        Ok(guard.get(&key).cloned())
    }

    fn patch_working_state(
        &self,
        scope: &Scope,
        patch: WorkingStatePatch,
    ) -> StoreResult<WorkingState> {
        let key = RunKey::from(scope);
        let mut guard = self.wm_state.write().map_err(|_| StoreError::Poisoned)?;
        let mut current = guard.get(&key).cloned().unwrap_or_default();

        let mut touched = false;
        if let Some(goal) = patch.goal {
            current.goal = goal;
            touched = true;
        }
        if let Some(plan) = patch.plan {
            current.plan = plan;
            touched = true;
        }
        if let Some(slots) = patch.slots {
            current.slots = slots;
            touched = true;
        }
        if let Some(constraints) = patch.constraints {
            current.constraints = constraints;
            touched = true;
        }
        if let Some(tool_evidence) = patch.tool_evidence {
            current.tool_evidence = tool_evidence;
            touched = true;
        }
        if let Some(decisions) = patch.decisions {
            current.decisions = decisions;
            touched = true;
        }
        if let Some(risks) = patch.risks {
            current.risks = risks;
            touched = true;
        }

        if let Some(state_version) = patch.state_version {
            current.state_version = state_version;
        } else if touched {
            current.state_version = current.state_version.saturating_add(1);
        }

        guard.insert(key, current.clone());
        Ok(current)
    }

    fn get_stm(&self, scope: &Scope) -> StoreResult<Option<StmState>> {
        let key = SessionKey::from(scope);
        let guard = self.stm_state.read().map_err(|_| StoreError::Poisoned)?;
        Ok(guard.get(&key).cloned())
    }

    fn update_stm(&self, scope: &Scope, stm: StmState) -> StoreResult<()> {
        let key = SessionKey::from(scope);
        let mut guard = self.stm_state.write().map_err(|_| StoreError::Poisoned)?;
        guard.insert(key, stm);
        Ok(())
    }

    fn list_facts(&self, scope: &Scope, filter: FactFilter) -> StoreResult<Vec<Fact>> {
        let key = LtmKey::from(scope);
        let guard = self.facts.read().map_err(|_| StoreError::Poisoned)?;
        let mut results: Vec<Fact> = guard
            .get(&key)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|f| match &filter.status {
                Some(statuses) => statuses.contains(&f.status),
                None => true,
            })
            .filter(|f| match filter.valid_at {
                Some(t) => {
                    let from_ok = f.validity.valid_from.map(|v| v <= t).unwrap_or(true);
                    let to_ok = f.validity.valid_to.map(|v| v >= t).unwrap_or(true);
                    from_ok && to_ok
                }
                None => true,
            })
            .collect();

        apply_limit(&mut results, filter.limit);
        Ok(results)
    }

    fn upsert_fact(&self, scope: &Scope, fact: Fact) -> StoreResult<()> {
        let key = LtmKey::from(scope);
        let mut guard = self.facts.write().map_err(|_| StoreError::Poisoned)?;
        let entry = guard.entry(key).or_insert_with(Vec::new);
        match entry.iter().position(|f| f.fact_id == fact.fact_id) {
            Some(idx) => entry[idx] = fact,
            None => entry.push(fact),
        }
        Ok(())
    }

    fn list_episodes(
        &self,
        scope: &Scope,
        filter: EpisodeFilter,
    ) -> StoreResult<Vec<memweft_types::Episode>> {
        let key = LtmKey::from(scope);
        let guard = self.episodes.read().map_err(|_| StoreError::Poisoned)?;
        let mut results: Vec<memweft_types::Episode> = guard
            .get(&key)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|e| match &filter.time_range {
                Some(range) => {
                    let start_ok = range.start.map(|s| e.time_range.start >= s).unwrap_or(true);
                    let end_ok = range
                        .end
                        .map(|end| {
                            let episode_end = e.time_range.end.unwrap_or(e.time_range.start);
                            episode_end <= end
                        })
                        .unwrap_or(true);
                    start_ok && end_ok
                }
                None => true,
            })
            .filter(|e| {
                if filter.tags.is_empty() {
                    true
                } else {
                    e.tags.iter().any(|t| filter.tags.contains(t))
                }
            })
            .filter(|e| {
                if filter.entities.is_empty() {
                    true
                } else {
                    e.entities.iter().any(|t| filter.entities.contains(t))
                }
            })
            .collect();

        apply_limit(&mut results, filter.limit);
        Ok(results)
    }

    fn append_episode(&self, scope: &Scope, episode: memweft_types::Episode) -> StoreResult<()> {
        let key = LtmKey::from(scope);
        let mut guard = self.episodes.write().map_err(|_| StoreError::Poisoned)?;
        guard.entry(key).or_insert_with(Vec::new).push(episode);
        Ok(())
    }

    fn list_procedures(
        &self,
        scope: &Scope,
        task_type: &str,
        limit: Option<usize>,
    ) -> StoreResult<Vec<Procedure>> {
        let key = LtmKey::from(scope);
        let guard = self.procedures.read().map_err(|_| StoreError::Poisoned)?;
        let mut results: Vec<Procedure> = guard
            .get(&key)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|p| p.task_type == task_type)
            .collect();

        apply_limit(&mut results, limit);
        Ok(results)
    }

    fn upsert_procedure(&self, scope: &Scope, procedure: Procedure) -> StoreResult<()> {
        let key = LtmKey::from(scope);
        let mut guard = self.procedures.write().map_err(|_| StoreError::Poisoned)?;
        let entry = guard.entry(key).or_insert_with(Vec::new);
        match entry.iter().position(|p| p.procedure_id == procedure.procedure_id) {
            Some(idx) => entry[idx] = procedure,
            None => entry.push(procedure),
        }
        Ok(())
    }

    fn list_insights(&self, scope: &Scope, filter: InsightFilter) -> StoreResult<Vec<InsightItem>> {
        let key = RunKey::from(scope);
        let guard = self.insights.read().map_err(|_| StoreError::Poisoned)?;
        let mut results: Vec<InsightItem> = guard
            .get(&key)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|i| match &filter.validation_state {
                Some(states) => states.contains(&i.validation_state),
                None => true,
            })
            .collect();

        apply_limit(&mut results, filter.limit);
        Ok(results)
    }

    fn append_insight(&self, scope: &Scope, insight: InsightItem) -> StoreResult<()> {
        let key = RunKey::from(scope);
        let mut guard = self.insights.write().map_err(|_| StoreError::Poisoned)?;
        guard.entry(key).or_insert_with(Vec::new).push(insight);
        Ok(())
    }

    fn write_context_build(&self, scope: &Scope, packet: MemoryPacket) -> StoreResult<()> {
        let key = RunKey::from(scope);
        let mut guard = self.context_builds.write().map_err(|_| StoreError::Poisoned)?;
        guard.entry(key).or_insert_with(Vec::new).push(packet);
        Ok(())
    }

    fn list_context_builds(
        &self,
        scope: &Scope,
        limit: Option<usize>,
    ) -> StoreResult<Vec<MemoryPacket>> {
        let key = RunKey::from(scope);
        let guard = self.context_builds.read().map_err(|_| StoreError::Poisoned)?;
        let mut results = guard.get(&key).cloned().unwrap_or_default();
        apply_limit(&mut results, limit);
        Ok(results)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct RunKey {
    tenant_id: String,
    user_id: String,
    agent_id: String,
    session_id: String,
    run_id: String,
}

impl From<&Scope> for RunKey {
    fn from(scope: &Scope) -> Self {
        Self {
            tenant_id: scope.tenant_id.clone(),
            user_id: scope.user_id.clone(),
            agent_id: scope.agent_id.clone(),
            session_id: scope.session_id.clone(),
            run_id: scope.run_id.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct SessionKey {
    tenant_id: String,
    user_id: String,
    agent_id: String,
    session_id: String,
}

impl From<&Scope> for SessionKey {
    fn from(scope: &Scope) -> Self {
        Self {
            tenant_id: scope.tenant_id.clone(),
            user_id: scope.user_id.clone(),
            agent_id: scope.agent_id.clone(),
            session_id: scope.session_id.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct LtmKey {
    tenant_id: String,
    user_id: String,
    agent_id: String,
}

impl From<&Scope> for LtmKey {
    fn from(scope: &Scope) -> Self {
        Self {
            tenant_id: scope.tenant_id.clone(),
            user_id: scope.user_id.clone(),
            agent_id: scope.agent_id.clone(),
        }
    }
}

fn scope_matches(a: &Scope, b: &Scope) -> bool {
    a.tenant_id == b.tenant_id
        && a.user_id == b.user_id
        && a.agent_id == b.agent_id
        && a.session_id == b.session_id
        && a.run_id == b.run_id
}

fn apply_limit<T>(items: &mut Vec<T>, limit: Option<usize>) {
    if let Some(n) = limit {
        if items.len() > n {
            items.truncate(n);
        }
    }
}

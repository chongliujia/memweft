#![allow(unsafe_op_in_unsafe_fn)]

use chrono::{DateTime, TimeZone, Utc};
use memweft_store::{
    build_memory_packet, BuildRequest, EpisodeFilter, Event, EventKind, FactFilter, InsightFilter,
    RecallCues, RecallPolicy, SqliteStore, Store, StoreError, StmState, TimeRangeFilter,
    WorkingStatePatch, StoreResult,
};
#[cfg(feature = "mysql")]
use memweft_store::MySqlStore;
#[cfg(feature = "postgres")]
use memweft_store::PostgresStore;
use memweft_types::{
    Budget, Episode, Fact, FactStatus, InsightItem, JsonMap, KeyQuote, MemoryPacket, Procedure,
    Purpose, Scope, ValidationState,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::sync::Arc;

#[pyclass]
struct MemWeftStore {
    inner: Arc<dyn Store>,
}

#[pymethods]
impl MemWeftStore {
    fn request(&self, py: Python<'_>, payload: String) -> PyResult<String> {
        let memory = memweft::Memory::from_store(self.inner.clone());
        py.allow_threads(move || {
            let value = memory.request(parse_json(&payload)?).map_err(store_error)?;
            to_json(&value)
        })
    }

    fn async_request<'p>(&self, py: Python<'p>, payload: String) -> PyResult<&'p PyAny> {
        let memory = memweft::Memory::from_store(self.inner.clone());
        pyo3_asyncio::tokio::future_into_py(py, async move {
            tokio::task::spawn_blocking(move || {
                let value = memory.request(parse_json(&payload)?).map_err(store_error)?;
                to_json(&value)
            }).await.map_err(py_error)?
        })
    }
    #[new]
    #[pyo3(signature = (path=None, backend=None, dsn=None, database=None, in_memory=false, sqlite_options=None))]
    fn new(
        path: Option<String>,
        backend: Option<String>,
        dsn: Option<String>,
        database: Option<String>,
        in_memory: bool,
        sqlite_options: Option<String>,
    ) -> PyResult<Self> {
        let options = sqlite_options.as_deref().map(parse_json::<memweft_store::SqliteOptions>).transpose()?;
        let store = open_store(path, backend, dsn, database, in_memory, options).map_err(store_error)?;
        Ok(Self { inner: Arc::from(store) })
    }

    #[staticmethod]
    fn in_memory() -> PyResult<Self> {
        let inner: Box<dyn Store> =
            Box::new(SqliteStore::new_in_memory().map_err(store_error)?);
        Ok(Self { inner: Arc::from(inner) })
    }

    fn append_event(&self, event_json: &str) -> PyResult<()> {
        let input: EventInput = parse_json(event_json)?;
        let event = input.to_event()?;
        self.inner.append_event(event).map_err(store_error)
    }

    fn async_append_event<'p>(&self, py: Python<'p>, event_json: String) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let input: EventInput = parse_json(&event_json)?;
            let event = input.to_event()?;
            tokio::task::spawn_blocking(move || {
                store.append_event(event).map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn list_events(
        &self,
        scope_json: &str,
        range_json: Option<&str>,
        limit: Option<usize>,
    ) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let range = match range_json {
            Some(payload) => parse_json::<TimeRangeInput>(payload)?.to_filter()?,
            None => TimeRangeFilter::default(),
        };
        let events = self
            .inner
            .list_events(&scope, range, limit)
            .map_err(store_error)?;
        let output: Vec<EventOutput> = events.into_iter().map(EventOutput::from).collect();
        to_json(&output)
    }

    fn async_list_events<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        range_json: Option<String>,
        limit: Option<usize>,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let range = match range_json {
                Some(payload) => parse_json::<TimeRangeInput>(&payload)?.to_filter()?,
                None => TimeRangeFilter::default(),
            };
            let json = tokio::task::spawn_blocking(move || {
                let events = store
                    .list_events(&scope, range, limit)
                    .map_err(store_error)?;
                let output: Vec<EventOutput> = events.into_iter().map(EventOutput::from).collect();
                to_json(&output)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn get_working_state(&self, scope_json: &str) -> PyResult<Option<String>> {
        let scope: Scope = parse_json(scope_json)?;
        let state = self.inner.get_working_state(&scope).map_err(store_error)?;
        match state {
            Some(state) => Ok(Some(to_json(&state)?)),
            None => Ok(None),
        }
    }

    fn async_get_working_state<'p>(&self, py: Python<'p>, scope_json: String) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let json = tokio::task::spawn_blocking(move || -> PyResult<Option<String>> {
                let state = store.get_working_state(&scope).map_err(store_error)?;
                match state {
                    Some(state) => Ok(Some(to_json(&state)?)),
                    None => Ok(None),
                }
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn patch_working_state(&self, scope_json: &str, patch_json: &str) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let patch_input: WorkingStatePatchInput = parse_json(patch_json)?;
        let patch = patch_input.to_patch();
        let state = self
            .inner
            .patch_working_state(&scope, patch)
            .map_err(store_error)?;
        to_json(&state)
    }

    fn async_patch_working_state<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        patch_json: String,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let patch_input: WorkingStatePatchInput = parse_json(&patch_json)?;
            let patch = patch_input.to_patch();
            let json = tokio::task::spawn_blocking(move || {
                let state = store
                    .patch_working_state(&scope, patch)
                    .map_err(store_error)?;
                to_json(&state)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn get_stm(&self, scope_json: &str) -> PyResult<Option<String>> {
        let scope: Scope = parse_json(scope_json)?;
        let state = self.inner.get_stm(&scope).map_err(store_error)?;
        match state {
            Some(state) => Ok(Some(to_json(&StmStateOutput::from(state))?)),
            None => Ok(None),
        }
    }

    fn async_get_stm<'p>(&self, py: Python<'p>, scope_json: String) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let json = tokio::task::spawn_blocking(move || -> PyResult<Option<String>> {
                let state = store.get_stm(&scope).map_err(store_error)?;
                match state {
                    Some(state) => Ok(Some(to_json(&StmStateOutput::from(state))?)),
                    None => Ok(None),
                }
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn update_stm(&self, scope_json: &str, stm_json: &str) -> PyResult<()> {
        let scope: Scope = parse_json(scope_json)?;
        let input: StmStateInput = parse_json(stm_json)?;
        let stm = StmState {
            rolling_summary: input.rolling_summary,
            key_quotes: input.key_quotes,
        };
        self.inner.update_stm(&scope, stm).map_err(store_error)
    }

    fn async_update_stm<'p>(&self, py: Python<'p>, scope_json: String, stm_json: String) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let input: StmStateInput = parse_json(&stm_json)?;
            let stm = StmState {
                rolling_summary: input.rolling_summary,
                key_quotes: input.key_quotes,
            };
            tokio::task::spawn_blocking(move || {
                store.update_stm(&scope, stm).map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn list_facts(&self, scope_json: &str, filter_json: Option<&str>) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let filter = match filter_json {
            Some(payload) => parse_json::<FactFilterInput>(payload)?.to_filter()?,
            None => FactFilter::default(),
        };
        let facts = self
            .inner
            .list_facts(&scope, filter)
            .map_err(store_error)?;
        to_json(&facts)
    }

    fn async_list_facts<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        filter_json: Option<String>,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let filter = match filter_json {
                Some(payload) => parse_json::<FactFilterInput>(&payload)?.to_filter()?,
                None => FactFilter::default(),
            };
            let json = tokio::task::spawn_blocking(move || {
                let facts = store
                    .list_facts(&scope, filter)
                    .map_err(store_error)?;
                to_json(&facts)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn upsert_fact(&self, scope_json: &str, fact_json: &str) -> PyResult<()> {
        let scope: Scope = parse_json(scope_json)?;
        let fact: Fact = parse_json(fact_json)?;
        self.inner.upsert_fact(&scope, fact).map_err(store_error)
    }

    fn async_upsert_fact<'p>(&self, py: Python<'p>, scope_json: String, fact_json: String) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let fact: Fact = parse_json(&fact_json)?;
            tokio::task::spawn_blocking(move || {
                store.upsert_fact(&scope, fact).map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn list_episodes(&self, scope_json: &str, filter_json: Option<&str>) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let filter = match filter_json {
            Some(payload) => parse_json::<EpisodeFilterInput>(payload)?.to_filter()?,
            None => EpisodeFilter::default(),
        };
        let episodes = self
            .inner
            .list_episodes(&scope, filter)
            .map_err(store_error)?;
        to_json(&episodes)
    }

    fn async_list_episodes<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        filter_json: Option<String>,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let filter = match filter_json {
                Some(payload) => parse_json::<EpisodeFilterInput>(&payload)?.to_filter()?,
                None => EpisodeFilter::default(),
            };
            let json = tokio::task::spawn_blocking(move || {
                let episodes = store
                    .list_episodes(&scope, filter)
                    .map_err(store_error)?;
                to_json(&episodes)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn append_episode(&self, scope_json: &str, episode_json: &str) -> PyResult<()> {
        let scope: Scope = parse_json(scope_json)?;
        let episode: Episode = parse_json(episode_json)?;
        self.inner
            .append_episode(&scope, episode)
            .map_err(store_error)
    }

    fn async_append_episode<'p>(&self, py: Python<'p>, scope_json: String, episode_json: String) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let episode: Episode = parse_json(&episode_json)?;
            tokio::task::spawn_blocking(move || {
                store
                    .append_episode(&scope, episode)
                    .map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn list_procedures(
        &self,
        scope_json: &str,
        task_type: &str,
        limit: Option<usize>,
    ) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let procedures = self
            .inner
            .list_procedures(&scope, task_type, limit)
            .map_err(store_error)?;
        to_json(&procedures)
    }

    fn async_list_procedures<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        task_type: String,
        limit: Option<usize>,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let json = tokio::task::spawn_blocking(move || {
                let procedures = store
                    .list_procedures(&scope, &task_type, limit)
                    .map_err(store_error)?;
                to_json(&procedures)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn upsert_procedure(&self, scope_json: &str, procedure_json: &str) -> PyResult<()> {
        let scope: Scope = parse_json(scope_json)?;
        let procedure: Procedure = parse_json(procedure_json)?;
        self.inner
            .upsert_procedure(&scope, procedure)
            .map_err(store_error)
    }

    fn async_upsert_procedure<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        procedure_json: String,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let procedure: Procedure = parse_json(&procedure_json)?;
            tokio::task::spawn_blocking(move || {
                store
                    .upsert_procedure(&scope, procedure)
                    .map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn list_insights(&self, scope_json: &str, filter_json: Option<&str>) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let filter = match filter_json {
            Some(payload) => parse_json::<InsightFilterInput>(payload)?.to_filter()?,
            None => InsightFilter::default(),
        };
        let insights = self
            .inner
            .list_insights(&scope, filter)
            .map_err(store_error)?;
        to_json(&insights)
    }

    fn async_list_insights<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        filter_json: Option<String>,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let filter = match filter_json {
                Some(payload) => parse_json::<InsightFilterInput>(&payload)?.to_filter()?,
                None => InsightFilter::default(),
            };
            let json = tokio::task::spawn_blocking(move || {
                let insights = store
                    .list_insights(&scope, filter)
                    .map_err(store_error)?;
                to_json(&insights)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn append_insight(&self, scope_json: &str, insight_json: &str) -> PyResult<()> {
        let scope: Scope = parse_json(scope_json)?;
        let insight: InsightItem = parse_json(insight_json)?;
        self.inner
            .append_insight(&scope, insight)
            .map_err(store_error)
    }

    fn async_append_insight<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        insight_json: String,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let insight: InsightItem = parse_json(&insight_json)?;
            tokio::task::spawn_blocking(move || {
                store
                    .append_insight(&scope, insight)
                    .map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn write_context_build(&self, scope_json: &str, packet_json: &str) -> PyResult<()> {
        let scope: Scope = parse_json(scope_json)?;
        let packet: MemoryPacket = parse_json(packet_json)?;
        self.inner
            .write_context_build(&scope, packet)
            .map_err(store_error)
    }

    fn async_write_context_build<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        packet_json: String,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let packet: MemoryPacket = parse_json(&packet_json)?;
            tokio::task::spawn_blocking(move || {
                store
                    .write_context_build(&scope, packet)
                    .map_err(store_error)
            }).await.map_err(py_error)??;
            Ok(())
        })
    }

    fn list_context_builds(&self, scope_json: &str, limit: Option<usize>) -> PyResult<String> {
        let scope: Scope = parse_json(scope_json)?;
        let packets = self
            .inner
            .list_context_builds(&scope, limit)
            .map_err(store_error)?;
        to_json(&packets)
    }

    fn async_list_context_builds<'p>(
        &self,
        py: Python<'p>,
        scope_json: String,
        limit: Option<usize>,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let scope: Scope = parse_json(&scope_json)?;
            let json = tokio::task::spawn_blocking(move || {
                let packets = store
                    .list_context_builds(&scope, limit)
                    .map_err(store_error)?;
                to_json(&packets)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }

    fn build_memory_packet(&self, request_json: &str) -> PyResult<String> {
        let input: BuildRequestInput = parse_json(request_json)?;
        let mut request = BuildRequest::new(input.scope, input.purpose);

        if let Some(task_type) = input.task_type {
            request.task_type = Some(task_type);
        }
        if let Some(cues) = input.cues {
            request.cues = cues.to_cues()?;
        }
        if let Some(budget) = input.budget {
            request.budget = budget;
        }
        if let Some(policy_id) = input.policy_id {
            request.policy_id = policy_id;
        }
        if let Some(policy) = input.policy {
            request.policy = policy.apply_to(RecallPolicy::default());
        }
        if let Some(persist) = input.persist {
            request.persist = persist;
        }

        let packet = build_memory_packet(self.inner.as_ref(), request).map_err(store_error)?;
        to_json(&packet)
    }

    fn async_build_memory_packet<'p>(
        &self,
        py: Python<'p>,
        request_json: String,
    ) -> PyResult<&'p PyAny> {
        let store = self.inner.clone();
        pyo3_asyncio::tokio::future_into_py(py, async move {
            let input: BuildRequestInput = parse_json(&request_json)?;
            let mut request = BuildRequest::new(input.scope, input.purpose);

            if let Some(task_type) = input.task_type {
                request.task_type = Some(task_type);
            }
            if let Some(cues) = input.cues {
                request.cues = cues.to_cues()?;
            }
            if let Some(budget) = input.budget {
                request.budget = budget;
            }
            if let Some(policy_id) = input.policy_id {
                request.policy_id = policy_id;
            }
            if let Some(policy) = input.policy {
                request.policy = policy.apply_to(RecallPolicy::default());
            }
            if let Some(persist) = input.persist {
                request.persist = persist;
            }

            let json = tokio::task::spawn_blocking(move || {
                let packet = build_memory_packet(store.as_ref(), request).map_err(store_error)?;
                to_json(&packet)
            }).await.map_err(py_error)??;
            Ok(json)
        })
    }
}

#[pymodule]
fn _core(_py: Python, module: &PyModule) -> PyResult<()> {
    pyo3_log::init();
    module.add_class::<MemWeftStore>()?;
    Ok(())
}

#[derive(Deserialize)]
struct EventInput {
    event_id: String,
    scope: Scope,
    #[serde(default)]
    ts: Option<String>,
    #[serde(default)]
    ts_ms: Option<i64>,
    kind: String,
    payload: JsonValue,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    entities: Vec<String>,
}

impl EventInput {
    fn to_event(self) -> PyResult<Event> {
        let ts = parse_timestamp(self.ts_ms, self.ts)?;
        let kind = parse_event_kind(&self.kind)?;
        Ok(Event {
            event_id: self.event_id,
            scope: self.scope,
            ts,
            kind,
            payload: self.payload,
            tags: self.tags,
            entities: self.entities,
        })
    }
}

#[derive(Serialize)]
struct EventOutput {
    event_id: String,
    scope: Scope,
    ts: String,
    kind: String,
    payload: JsonValue,
    tags: Vec<String>,
    entities: Vec<String>,
}

impl From<Event> for EventOutput {
    fn from(event: Event) -> Self {
        Self {
            event_id: event.event_id,
            scope: event.scope,
            ts: event.ts.to_rfc3339(),
            kind: event_kind_to_str(&event.kind).to_string(),
            payload: event.payload,
            tags: event.tags,
            entities: event.entities,
        }
    }
}

#[derive(Deserialize, Default)]
struct TimeRangeInput {
    #[serde(default)]
    start: Option<String>,
    #[serde(default)]
    end: Option<String>,
    #[serde(default)]
    start_ms: Option<i64>,
    #[serde(default)]
    end_ms: Option<i64>,
}

impl TimeRangeInput {
    fn to_filter(self) -> PyResult<TimeRangeFilter> {
        Ok(TimeRangeFilter {
            start: parse_optional_timestamp(self.start_ms, self.start)?,
            end: parse_optional_timestamp(self.end_ms, self.end)?,
        })
    }
}

#[derive(Deserialize, Default)]
struct FactFilterInput {
    #[serde(default)]
    status: Option<Vec<FactStatus>>,
    #[serde(default)]
    valid_at: Option<String>,
    #[serde(default)]
    valid_at_ms: Option<i64>,
    #[serde(default)]
    limit: Option<usize>,
}

impl FactFilterInput {
    fn to_filter(self) -> PyResult<FactFilter> {
        Ok(FactFilter {
            status: self.status,
            valid_at: parse_optional_timestamp(self.valid_at_ms, self.valid_at)?,
            limit: self.limit,
        })
    }
}

#[derive(Deserialize, Default)]
struct EpisodeFilterInput {
    #[serde(default)]
    time_range: Option<TimeRangeInput>,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    entities: Vec<String>,
    #[serde(default)]
    limit: Option<usize>,
}

impl EpisodeFilterInput {
    fn to_filter(self) -> PyResult<EpisodeFilter> {
        Ok(EpisodeFilter {
            time_range: match self.time_range {
                Some(range) => Some(range.to_filter()?),
                None => None,
            },
            tags: self.tags,
            entities: self.entities,
            limit: self.limit,
        })
    }
}

#[derive(Deserialize, Default)]
struct InsightFilterInput {
    #[serde(default)]
    validation_state: Option<Vec<ValidationState>>,
    #[serde(default)]
    limit: Option<usize>,
}

impl InsightFilterInput {
    fn to_filter(self) -> PyResult<InsightFilter> {
        Ok(InsightFilter {
            validation_state: self.validation_state,
            limit: self.limit,
        })
    }
}

#[derive(Deserialize, Default)]
struct StmStateInput {
    #[serde(default)]
    rolling_summary: String,
    #[serde(default)]
    key_quotes: Vec<KeyQuote>,
}

#[derive(Serialize)]
struct StmStateOutput {
    rolling_summary: String,
    key_quotes: Vec<KeyQuote>,
}

impl From<StmState> for StmStateOutput {
    fn from(state: StmState) -> Self {
        Self {
            rolling_summary: state.rolling_summary,
            key_quotes: state.key_quotes,
        }
    }
}

#[derive(Deserialize, Default)]
struct WorkingStatePatchInput {
    #[serde(default)]
    goal: Option<String>,
    #[serde(default)]
    plan: Option<Vec<String>>,
    #[serde(default)]
    slots: Option<JsonMap>,
    #[serde(default)]
    constraints: Option<JsonMap>,
    #[serde(default)]
    tool_evidence: Option<Vec<memweft_types::EvidenceRef>>,
    #[serde(default)]
    decisions: Option<Vec<String>>,
    #[serde(default)]
    risks: Option<Vec<String>>,
    #[serde(default)]
    state_version: Option<u32>,
}

impl WorkingStatePatchInput {
    fn to_patch(self) -> WorkingStatePatch {
        WorkingStatePatch {
            goal: self.goal,
            plan: self.plan,
            slots: self.slots,
            constraints: self.constraints,
            tool_evidence: self.tool_evidence,
            decisions: self.decisions,
            risks: self.risks,
            state_version: self.state_version,
        }
    }
}

#[derive(Deserialize)]
struct BuildRequestInput {
    scope: Scope,
    purpose: Purpose,
    #[serde(default)]
    task_type: Option<String>,
    #[serde(default)]
    cues: Option<RecallCuesInput>,
    #[serde(default)]
    budget: Option<Budget>,
    #[serde(default)]
    policy_id: Option<String>,
    #[serde(default)]
    policy: Option<RecallPolicyInput>,
    #[serde(default)]
    persist: Option<bool>,
}

#[derive(Deserialize, Default)]
struct RecallCuesInput {
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    entities: Vec<String>,
    #[serde(default)]
    keywords: Vec<String>,
    #[serde(default)]
    time_range: Option<TimeRangeInput>,
}

impl RecallCuesInput {
    fn to_cues(self) -> PyResult<RecallCues> {
        Ok(RecallCues {
            tags: self.tags,
            entities: self.entities,
            keywords: self.keywords,
            time_range: match self.time_range {
                Some(range) => Some(range.to_filter()?),
                None => None,
            },
        })
    }
}

#[derive(Deserialize, Default)]
struct RecallPolicyInput {
    #[serde(default)]
    max_total_candidates: Option<usize>,
    #[serde(default)]
    max_facts: Option<usize>,
    #[serde(default)]
    max_procedures: Option<usize>,
    #[serde(default)]
    max_episodes: Option<usize>,
    #[serde(default)]
    max_insights: Option<usize>,
    #[serde(default)]
    max_key_quotes: Option<usize>,
    #[serde(default)]
    conversation_window: Option<usize>,
    #[serde(default)]
    episode_time_window_days: Option<i64>,
    #[serde(default)]
    last_tool_evidence_limit: Option<usize>,
    #[serde(default)]
    include_conversation_window: Option<bool>,
    #[serde(default)]
    include_insights_in_tool: Option<bool>,
    #[serde(default)]
    allow_insights_in_responder: Option<bool>,
}

impl RecallPolicyInput {
    fn apply_to(self, mut policy: RecallPolicy) -> RecallPolicy {
        if let Some(value) = self.max_total_candidates {
            policy.max_total_candidates = value;
        }
        if let Some(value) = self.max_facts {
            policy.max_facts = value;
        }
        if let Some(value) = self.max_procedures {
            policy.max_procedures = value;
        }
        if let Some(value) = self.max_episodes {
            policy.max_episodes = value;
        }
        if let Some(value) = self.max_insights {
            policy.max_insights = value;
        }
        if let Some(value) = self.max_key_quotes {
            policy.max_key_quotes = value;
        }
        if let Some(value) = self.conversation_window {
            policy.conversation_window = value;
        }
        if let Some(value) = self.episode_time_window_days {
            policy.episode_time_window_days = value;
        }
        if let Some(value) = self.last_tool_evidence_limit {
            policy.last_tool_evidence_limit = value;
        }
        if let Some(value) = self.include_conversation_window {
            policy.include_conversation_window = value;
        }
        if let Some(value) = self.include_insights_in_tool {
            policy.include_insights_in_tool = value;
        }
        if let Some(value) = self.allow_insights_in_responder {
            policy.allow_insights_in_responder = value;
        }
        policy
    }
}

fn parse_json<T: DeserializeOwned>(payload: &str) -> PyResult<T> {
    serde_json::from_str(payload).map_err(py_error)
}

fn to_json<T: Serialize>(value: &T) -> PyResult<String> {
    serde_json::to_string(value).map_err(py_error)
}

fn parse_timestamp(ts_ms: Option<i64>, ts: Option<String>) -> PyResult<DateTime<Utc>> {
    match (ts_ms, ts) {
        (Some(ms), _) => parse_millis(ms),
        (None, Some(text)) => parse_rfc3339(&text),
        (None, None) => Ok(Utc::now()),
    }
}

fn parse_optional_timestamp(
    ts_ms: Option<i64>,
    ts: Option<String>,
) -> PyResult<Option<DateTime<Utc>>> {
    match (ts_ms, ts) {
        (Some(ms), _) => Ok(Some(parse_millis(ms)?)),
        (None, Some(text)) => Ok(Some(parse_rfc3339(&text)?)),
        (None, None) => Ok(None),
    }
}

fn parse_millis(ms: i64) -> PyResult<DateTime<Utc>> {
    Utc.timestamp_millis_opt(ms)
        .single()
        .ok_or_else(|| PyValueError::new_err("invalid millisecond timestamp"))
}

fn parse_rfc3339(value: &str) -> PyResult<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .map(|dt| dt.with_timezone(&Utc))
        .map_err(py_error)
}

fn parse_event_kind(value: &str) -> PyResult<EventKind> {
    match value {
        "message" => Ok(EventKind::Message),
        "tool_result" => Ok(EventKind::ToolResult),
        "state_patch" => Ok(EventKind::StatePatch),
        "system" => Ok(EventKind::System),
        _ => Err(PyValueError::new_err("invalid event kind")),
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

fn store_error(err: StoreError) -> PyErr {
    PyValueError::new_err(match err {
        StoreError::NotFound => "store item not found".to_string(),
        StoreError::Poisoned => "store lock poisoned".to_string(),
        StoreError::InvalidInput(message) => format!("invalid input: {}", message),
        StoreError::Storage(message) => format!("storage error: {}", message),
        StoreError::Conflict(message) => format!("conflict: {}", message),
    })
}

fn py_error<E: std::fmt::Display>(err: E) -> PyErr {
    PyValueError::new_err(err.to_string())
}

fn open_store(
    path: Option<String>,
    backend: Option<String>,
    dsn: Option<String>,
    database: Option<String>,
    in_memory: bool,
    sqlite_options: Option<memweft_store::SqliteOptions>,
) -> StoreResult<Box<dyn Store>> {
    let backend = backend
        .unwrap_or_else(|| "sqlite".to_string())
        .to_lowercase();
    if backend != "sqlite" && sqlite_options.is_some() {
        return Err(StoreError::InvalidInput("sqlite_options requires sqlite backend".into()));
    }
    match backend.as_str() {
        "sqlite" => {
            let path = if in_memory { ":memory:".into() } else {
                path.unwrap_or_else(|| "data/memweft.db".to_string())
            };
            Ok(Box::new(SqliteStore::new_with_options(path, sqlite_options.unwrap_or_default())?))
        }
        "postgres" => {
            if in_memory {
                return Err(StoreError::InvalidInput(
                    "in_memory only supported for sqlite".to_string(),
                ));
            }
            let dsn = dsn.ok_or_else(|| {
                StoreError::InvalidInput("dsn required for postgres backend".to_string())
            })?;
            #[cfg(feature = "postgres")]
            {
                let dsn = apply_database_to_dsn(&dsn, database.as_deref());
                return Ok(Box::new(PostgresStore::new(&dsn)?));
            }
            #[cfg(not(feature = "postgres"))]
            {
                let _ = (dsn, database);
                return Err(StoreError::InvalidInput(
                    "postgres feature not enabled".to_string(),
                ));
            }
        }
        "mysql" => {
            if in_memory {
                return Err(StoreError::InvalidInput(
                    "in_memory only supported for sqlite".to_string(),
                ));
            }
            let dsn =
                dsn.ok_or_else(|| StoreError::InvalidInput("dsn required for mysql backend".to_string()))?;
            #[cfg(feature = "mysql")]
            {
                let dsn = apply_database_to_dsn(&dsn, database.as_deref());
                return Ok(Box::new(MySqlStore::new(&dsn)?));
            }
            #[cfg(not(feature = "mysql"))]
            {
                let _ = (dsn, database);
                return Err(StoreError::InvalidInput(
                    "mysql feature not enabled".to_string(),
                ));
            }
        }
        _ => Err(StoreError::InvalidInput(format!(
            "unknown backend: {}",
            backend
        ))),
    }
}

#[cfg(any(feature = "mysql", feature = "postgres"))]
fn apply_database_to_dsn(dsn: &str, database: Option<&str>) -> String {
    let Some(database) = database else {
        return dsn.to_string();
    };
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

#[cfg(any(feature = "mysql", feature = "postgres"))]
fn dsn_has_database(dsn: &str) -> bool {
    let base = dsn.split('?').next().unwrap_or(dsn);
    let scheme_end = base.find("://").map(|idx| idx + 3).unwrap_or(0);
    match base[scheme_end..].find('/') {
        Some(idx) => scheme_end + idx + 1 < base.len(),
        None => false,
    }
}

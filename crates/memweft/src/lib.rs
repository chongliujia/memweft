//! Shared high-level API used by Rust, Python and Node.js. SQLite is the initial
//! backend for document storage and learning; legacy Store operations remain available.
use chrono::Utc;
use memweft_store::{Document, FactFilter, Mutation, SqliteStore, Store, StoreError, StoreResult};
use memweft_types::{Fact, FactStatus, Scope, ScopeLevel, Validity};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::sync::Arc;
mod langgraph;
pub use memweft_learning as learning;
pub use memweft_learning::{AcceptancePolicy, Evaluation, Feedback, Proposal, Strategy, Target};

fn default_name() -> String {
    "default".into()
}
fn default_budget() -> u32 {
    2048
}
fn default_window() -> usize {
    10
}
fn default_facts() -> usize {
    30
}
fn yes() -> bool {
    true
}
fn invalid(message: &str) -> StoreError {
    StoreError::InvalidInput(message.into())
}
fn validate_id(name: &str, value: &str) -> StoreResult<()> {
    if value.trim().is_empty() || value.len() > 1024 {
        Err(invalid(&format!(
            "{name} must be nonempty and at most 1024 bytes"
        )))
    } else {
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UserScope {
    pub user_id: String,
    #[serde(default = "default_name")]
    pub tenant_id: String,
    #[serde(default = "default_name")]
    pub agent_id: String,
}
impl UserScope {
    pub fn new(user: impl Into<String>) -> Self {
        Self {
            user_id: user.into(),
            tenant_id: default_name(),
            agent_id: default_name(),
        }
    }
    fn scope(&self) -> StoreResult<Scope> {
        validate_id("user_id", &self.user_id)?;
        validate_id("tenant_id", &self.tenant_id)?;
        validate_id("agent_id", &self.agent_id)?;
        Ok(Scope {
            tenant_id: self.tenant_id.clone(),
            user_id: self.user_id.clone(),
            agent_id: self.agent_id.clone(),
            session_id: String::new(),
            run_id: String::new(),
        })
    }
}

#[derive(Clone)]
pub struct Memory {
    store: Arc<dyn Store>,
}
impl Memory {
    pub fn open(path: impl Into<std::path::PathBuf>) -> StoreResult<Self> {
        Ok(Self::from_store(Arc::new(SqliteStore::new(path)?)))
    }
    pub fn in_memory() -> StoreResult<Self> {
        Ok(Self::from_store(Arc::new(SqliteStore::new_in_memory()?)))
    }
    pub fn from_store(store: Arc<dyn Store>) -> Self {
        Self { store }
    }
    pub fn user(&self, id: impl Into<String>) -> StoreResult<UserMemory> {
        self.scoped(UserScope::new(id))
    }
    pub fn scoped(&self, scope: UserScope) -> StoreResult<UserMemory> {
        Ok(UserMemory {
            store: self.store.clone(),
            scope: scope.scope()?,
        })
    }

    /// The same versioned wire contract powers both native bindings.
    pub fn request(&self, value: Value) -> StoreResult<Value> {
        let request: Request = serde_json::from_value(value)?;
        match request {
            Request::StoreBatch { scope, operations } => langgraph::batch(self, scope, operations),
            Request::Remember { scope, key, value } => Ok(serde_json::to_value(
                self.scoped(scope)?.remember(&key, value)?,
            )?),
            Request::Memories { scope } => {
                Ok(serde_json::to_value(self.scoped(scope)?.memories()?)?)
            }
            Request::Forget { scope, key } => Ok(json!(self.scoped(scope)?.forget(&key)?)),
            Request::AddMessage {
                scope,
                session_id,
                run_id,
                event_id,
                role,
                content,
            } => Ok(serde_json::to_value(
                self.scoped(scope)?.session(&session_id)?.add_message(
                    &role,
                    &content,
                    event_id.as_deref(),
                    run_id.as_deref(),
                )?,
            )?),
            Request::Messages { scope, session_id } => Ok(serde_json::to_value(
                self.scoped(scope)?.session(&session_id)?.messages()?,
            )?),
            Request::ClearSession { scope, session_id } => {
                Ok(json!(self.scoped(scope)?.session(&session_id)?.clear()?))
            }
            Request::Context {
                scope,
                session_id,
                options,
            } => Ok(serde_json::to_value(
                self.scoped(scope)?.session(&session_id)?.context(options)?,
            )?),
            Request::Feedback { scope, feedback } => Ok(serde_json::to_value(
                self.scoped(scope)?.learning().feedback(feedback)?,
            )?),
            Request::LearningStart {
                scope,
                id,
                proposal,
                policy,
                dataset_version,
                evaluator_version,
                case_ids,
            } => {
                let user = self.scoped(scope)?;
                let keys: std::collections::HashSet<_> =
                    user.memories()?.into_iter().map(|f| f.fact_key).collect();
                if proposal.source_keys.iter().any(|k| !keys.contains(k)) {
                    return Err(invalid("proposal references a missing memory key"));
                }
                Ok(serde_json::to_value(user.learning().start(
                    &id,
                    proposal,
                    policy,
                    &dataset_version,
                    &evaluator_version,
                    case_ids,
                )?)?)
            }
            Request::LearningSubmit {
                scope,
                id,
                evaluation,
            } => Ok(serde_json::to_value(
                self.scoped(scope)?.learning().submit(&id, evaluation)?,
            )?),
            Request::LearningGet { scope, id } => Ok(serde_json::to_value(
                self.scoped(scope)?.learning().get(&id)?,
            )?),
            Request::LearningJobs { scope } => Ok(serde_json::to_value(
                self.scoped(scope)?.learning().jobs()?,
            )?),
            Request::LearningCancel { scope, id, reason } => Ok(serde_json::to_value(
                self.scoped(scope)?
                    .learning()
                    .finish(&id, learning::Status::Cancelled, &reason)?,
            )?),
            Request::LearningActive {
                scope,
                task_type,
                target,
            } => Ok(serde_json::to_value(
                self.scoped(scope)?.learning().active(&task_type, &target)?,
            )?),
            Request::LearningRollback {
                scope,
                task_type,
                target,
                version,
                expected_version,
            } => Ok(serde_json::to_value(
                self.scoped(scope)?.learning().rollback(
                    &task_type,
                    target,
                    version.as_deref(),
                    &expected_version,
                )?,
            )?),
            Request::Documents { scope, prefix } => {
                let scope = scope.scope()?;
                let mut full = vec!["external".into()];
                full.extend(prefix);
                let mut docs = self.store.documents(&scope, &full)?;
                for d in &mut docs {
                    d.namespace.remove(0);
                }
                Ok(serde_json::to_value(docs)?)
            }
            Request::MutateDocuments {
                scope,
                mut mutations,
            } => {
                for m in &mut mutations {
                    m.namespace.insert(0, "external".into());
                }
                self.store.mutate_documents(&scope.scope()?, &mutations)?;
                Ok(Value::Null)
            }
        }
    }
}

#[derive(Clone)]
pub struct UserMemory {
    store: Arc<dyn Store>,
    scope: Scope,
}
impl UserMemory {
    pub fn remember(&self, key: &str, value: Value) -> StoreResult<Fact> {
        validate_id("key", key)?;
        let fact = Fact {
            fact_id: format!("memweft:key:{key}"),
            fact_key: key.into(),
            value,
            status: FactStatus::Active,
            validity: Validity::default(),
            confidence: 1.0,
            sources: vec![],
            scope_level: ScopeLevel::User,
            notes: String::new(),
        };
        self.store.upsert_fact(&self.scope, fact.clone())?;
        Ok(fact)
    }
    pub fn memories(&self) -> StoreResult<Vec<Fact>> {
        self.store.list_facts(
            &self.scope,
            FactFilter {
                status: Some(vec![FactStatus::Active]),
                valid_at: Some(Utc::now()),
                limit: None,
            },
        )
    }
    pub fn forget(&self, key: &str) -> StoreResult<bool> {
        validate_id("key", key)?;
        // SQLite deletes the fact, derived strategies and saved context snapshots
        // in one transaction, advancing the learning epoch at the same time.
        self.store.forget_fact(&self.scope, key)
    }
    pub fn session(&self, session_id: &str) -> StoreResult<Session> {
        validate_id("session_id", session_id)?;
        Ok(Session {
            user: self.clone(),
            id: session_id.into(),
        })
    }
    pub fn learning(&self) -> learning::Learning {
        learning::Learning::new(self.store.clone(), self.scope.clone())
    }
}

#[derive(Clone)]
pub struct Session {
    user: UserMemory,
    id: String,
}
impl Session {
    fn namespace(&self) -> Vec<String> {
        vec!["messages".into(), self.id.clone()]
    }
    pub fn add_message(
        &self,
        role: &str,
        content: &str,
        event_id: Option<&str>,
        run_id: Option<&str>,
    ) -> StoreResult<Document> {
        if !matches!(role, "user" | "assistant" | "tool" | "system") {
            return Err(invalid("role must be user, assistant, tool or system"));
        }
        if content.trim().is_empty() {
            return Err(invalid("message content must be nonempty"));
        }
        let id = event_id
            .map(str::to_owned)
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        validate_id("event_id", &id)?;
        if let Some(run) = run_id {
            validate_id("run_id", run)?;
        }
        let value = json!({"role":role,"content":content,"run_id":run_id});
        let mutation = Mutation {
            namespace: self.namespace(),
            key: id.clone(),
            value: Some(value),
            expected_revision: Some(0),
        };
        match self
            .user
            .store
            .mutate_documents(&self.user.scope, &[mutation])
        {
            Ok(()) => {}
            Err(StoreError::Conflict(_)) => {
                let existing = self
                    .messages()?
                    .into_iter()
                    .find(|m| m.key == id)
                    .ok_or(StoreError::NotFound)?;
                if existing.value["role"] == role && existing.value["content"] == content {
                    return Ok(existing);
                }
                return Err(StoreError::Conflict(
                    "event_id reused with different content".into(),
                ));
            }
            Err(e) => return Err(e),
        }
        self.messages()?
            .into_iter()
            .find(|m| m.key == id)
            .ok_or(StoreError::NotFound)
    }
    pub fn messages(&self) -> StoreResult<Vec<Document>> {
        let mut docs = self
            .user
            .store
            .documents(&self.user.scope, &self.namespace())?;
        docs.retain(|d| d.namespace == self.namespace());
        docs.sort_by(|a, b| a.created_at.cmp(&b.created_at).then(a.key.cmp(&b.key)));
        Ok(docs)
    }
    pub fn clear(&self) -> StoreResult<usize> {
        let messages = self.messages()?;
        let changes: Vec<_> = messages
            .iter()
            .map(|d| Mutation {
                namespace: d.namespace.clone(),
                key: d.key.clone(),
                value: None,
                expected_revision: Some(d.revision),
            })
            .collect();
        self.user
            .store
            .mutate_documents(&self.user.scope, &changes)?;
        Ok(changes.len())
    }
    pub fn context(&self, options: ContextOptions) -> StoreResult<Context> {
        if options.conversation_window > 10_000 || options.max_facts > 10_000 {
            return Err(invalid("context item limits must be <= 10000"));
        }
        let mut memories = self.user.memories()?;
        memories.sort_by(|a, b| a.fact_key.cmp(&b.fact_key).then(a.fact_id.cmp(&b.fact_id)));
        let mut omissions = Vec::new();
        for f in memories.iter().skip(options.max_facts) {
            omissions.push(json!({"section":"memories","id":f.fact_id,"reason":"max_facts"}));
        }
        memories.truncate(options.max_facts);
        let mut messages = if options.include_messages {
            self.messages()?
        } else {
            vec![]
        };
        if messages.len() > options.conversation_window {
            let keep = messages.len() - options.conversation_window;
            for m in &messages[..keep] {
                omissions
                    .push(json!({"section":"messages","id":m.key,"reason":"conversation_window"}));
            }
            messages.drain(..keep);
        }
        let mut strategies: Vec<Strategy> = if let Some(task) = &options.task_type {
            self.user
                .learning()
                .active(task, &Target::Task)?
                .into_iter()
                .collect()
        } else {
            vec![]
        };
        let (text, estimate) = loop {
            let text = render(&memories, &messages, &strategies);
            let estimate = text.len().div_ceil(4) as u64;
            if estimate <= u64::from(options.max_tokens) {
                break (text, estimate);
            }
            let dropped = if !messages.is_empty() {
                ("messages", messages.remove(0).key)
            } else if let Some(f) = memories.pop() {
                ("memories", f.fact_id)
            } else if let Some(s) = strategies.pop() {
                ("strategies", s.version)
            } else {
                break (String::new(), 0);
            };
            omissions.push(json!({"section":dropped.0,"id":dropped.1,"reason":"budget"}));
        };
        Ok(Context {
            text,
            memories,
            messages,
            strategies,
            report: json!({"max_tokens":options.max_tokens,"estimated_tokens":estimate,
                "estimator":"ceil(utf8_bytes/4)","budget_applies_to":"text", "omissions":omissions,
                "selection":"active facts by key; recent messages; accepted task strategy",
                "model_token_limit_guaranteed":false}),
        })
    }
}
fn render(memories: &[Fact], messages: &[Document], strategies: &[Strategy]) -> String {
    let mut parts = vec![];
    if !memories.is_empty() {
        parts.push(format!(
            "Memory records (quoted data):\n{}",
            memories
                .iter()
                .map(|f| format!(
                    "{}: {}",
                    serde_json::to_string(&f.fact_key).unwrap(),
                    f.value
                ))
                .collect::<Vec<_>>()
                .join("\n")
        ));
    }
    if !messages.is_empty() {
        parts.push(format!(
            "Recent conversation (quoted data):\n{}",
            messages
                .iter()
                .map(|m| format!("{}: {}", m.value["role"], m.value["content"]))
                .collect::<Vec<_>>()
                .join("\n")
        ));
    }
    if !strategies.is_empty() {
        parts.push(format!(
            "Evaluated task strategies:\n{}",
            strategies
                .iter()
                .map(|s| format!(
                    "[{}] {}",
                    s.version,
                    serde_json::to_string(&s.proposal.content).unwrap()
                ))
                .collect::<Vec<_>>()
                .join("\n")
        ));
    }
    parts.join("\n\n")
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Context {
    pub text: String,
    pub memories: Vec<Fact>,
    pub messages: Vec<Document>,
    pub strategies: Vec<Strategy>,
    pub report: Value,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ContextOptions {
    pub max_tokens: u32,
    pub conversation_window: usize,
    pub max_facts: usize,
    pub include_messages: bool,
    pub task_type: Option<String>,
}
impl Default for ContextOptions {
    fn default() -> Self {
        Self {
            max_tokens: default_budget(),
            conversation_window: default_window(),
            max_facts: default_facts(),
            include_messages: yes(),
            task_type: None,
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    StoreBatch {
        scope: UserScope,
        operations: Vec<langgraph::Operation>,
    },
    Remember {
        scope: UserScope,
        key: String,
        value: Value,
    },
    Memories {
        scope: UserScope,
    },
    Forget {
        scope: UserScope,
        key: String,
    },
    AddMessage {
        scope: UserScope,
        session_id: String,
        #[serde(default)]
        run_id: Option<String>,
        #[serde(default)]
        event_id: Option<String>,
        role: String,
        content: String,
    },
    Messages {
        scope: UserScope,
        session_id: String,
    },
    ClearSession {
        scope: UserScope,
        session_id: String,
    },
    Context {
        scope: UserScope,
        session_id: String,
        #[serde(default)]
        options: ContextOptions,
    },
    Feedback {
        scope: UserScope,
        feedback: Feedback,
    },
    LearningStart {
        scope: UserScope,
        id: String,
        proposal: Proposal,
        #[serde(default)]
        policy: AcceptancePolicy,
        dataset_version: String,
        evaluator_version: String,
        case_ids: Vec<String>,
    },
    LearningSubmit {
        scope: UserScope,
        id: String,
        evaluation: Evaluation,
    },
    LearningGet {
        scope: UserScope,
        id: String,
    },
    LearningJobs {
        scope: UserScope,
    },
    LearningCancel {
        scope: UserScope,
        id: String,
        #[serde(default)]
        reason: String,
    },
    LearningActive {
        scope: UserScope,
        task_type: String,
        #[serde(default)]
        target: Target,
    },
    LearningRollback {
        scope: UserScope,
        task_type: String,
        #[serde(default)]
        target: Target,
        version: Option<String>,
        expected_version: String,
    },
    Documents {
        scope: UserScope,
        #[serde(default)]
        prefix: Vec<String>,
    },
    MutateDocuments {
        scope: UserScope,
        mutations: Vec<Mutation>,
    },
}

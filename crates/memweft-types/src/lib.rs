use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub type JsonMap = BTreeMap<String, serde_json::Value>;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LongTerm {
    #[serde(default)]
    pub facts: Vec<Fact>,
    #[serde(default)]
    pub preferences: Vec<Fact>,
    #[serde(default)]
    pub procedures: Vec<Procedure>,
    #[serde(default)]
    pub episodes: Vec<Episode>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Fact {
    pub fact_id: String,
    pub fact_key: String,
    pub value: serde_json::Value,
    #[serde(default = "default_fact_status")]
    pub status: FactStatus,
    #[serde(default)]
    pub validity: Validity,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default = "default_scope_level")]
    pub scope_level: ScopeLevel,
    #[serde(default)]
    pub notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryPacket {
    pub meta: Meta,
    pub short_term: ShortTerm,
    pub long_term: LongTerm,
    #[serde(default)]
    pub insight: Insight,
    #[serde(default)]
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub budget_report: BudgetReport,
    #[serde(default)]
    pub explain: JsonMap,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Meta {
    #[serde(default = "default_schema_version")]
    pub schema_version: String,
    pub scope: Scope,
    #[serde(default = "now")]
    pub generated_at: DateTime<Utc>,
    pub purpose: Purpose,
    #[serde(default)]
    pub task_type: String,
    #[serde(default)]
    pub cues: JsonMap,
    pub budget: Budget,
    #[serde(default)]
    pub policy_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Scope {
    #[serde(default = "default_tenant_id")]
    pub tenant_id: String,
    pub user_id: String,
    pub agent_id: String,
    pub session_id: String,
    pub run_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Purpose {
    Planner,
    Tool,
    Responder,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Budget {
    pub max_tokens: u32,
    #[serde(default)]
    pub per_section: JsonMap,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ShortTerm {
    #[serde(default)]
    pub working_state: WorkingState,
    #[serde(default)]
    pub rolling_summary: String,
    #[serde(default)]
    pub key_quotes: Vec<KeyQuote>,
    #[serde(default)]
    pub conversation_window: Vec<ConversationTurn>,
    #[serde(default)]
    pub open_loops: Vec<String>,
    #[serde(default)]
    pub last_tool_evidence: Vec<EvidenceRef>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct WorkingState {
    #[serde(default)]
    pub goal: String,
    #[serde(default)]
    pub plan: Vec<String>,
    #[serde(default)]
    pub slots: JsonMap,
    #[serde(default)]
    pub constraints: JsonMap,
    #[serde(default)]
    pub tool_evidence: Vec<EvidenceRef>,
    #[serde(default)]
    pub decisions: Vec<String>,
    #[serde(default)]
    pub risks: Vec<String>,
    #[serde(default)]
    pub state_version: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KeyQuote {
    pub evidence_id: String,
    pub quote: String,
    #[serde(default = "default_role_user")]
    pub role: Role,
    #[serde(default)]
    pub ts: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConversationTurn {
    #[serde(default = "default_role_user")]
    pub role: Role,
    pub content: String,
    #[serde(default)]
    pub evidence_id: Option<String>,
    #[serde(default)]
    pub ts: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Validity {
    #[serde(default)]
    pub valid_from: Option<DateTime<Utc>>,
    #[serde(default)]
    pub valid_to: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FactStatus {
    Active,
    Disputed,
    Deprecated,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScopeLevel {
    User,
    Agent, 
    Tenant,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Procedure {
    pub procedure_id: String,
    pub task_type: String,
    pub content: serde_json::Value,
    #[serde(default)]
    pub priority: i32,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default)]
    pub applicability: JsonMap,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Episode {
    pub episode_id: String,
    pub time_range: TimeRange,
    pub summary: String,
    #[serde(default)]
    pub highlights: Vec<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub entities: Vec<String>,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default = "default_compression_level")]
    pub compression_level: CompressionLevel,
    #[serde(default)]
    pub recency_score: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimeRange {
    pub start: DateTime<Utc>,
    #[serde(default)]
    pub end: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompressionLevel {
    Raw,
    PhaseSummary,
    Milestone,
    Theme,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvidenceRef {
    pub evidence_id: String,
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Insight {
    #[serde(default)]
    pub usage_policy: UsagePolicy,
    #[serde(default)]
    pub hypotheses: Vec<InsightItem>,
    #[serde(default)]
    pub strategy_sketches: Vec<InsightItem>,
    #[serde(default)]
    pub patterns: Vec<InsightItem>,
}

impl Default for Insight {
    fn default() -> Self {
        Self {
            usage_policy: UsagePolicy::default(),
            hypotheses: Vec::new(),
            strategy_sketches: Vec::new(),
            patterns: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UsagePolicy {
    #[serde(default)]
    pub allow_in_responder: bool,
}

impl Default for UsagePolicy {
    fn default() -> Self {
        Self {
            allow_in_responder: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InsightItem {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: InsightType,
    pub statement: String,
    #[serde(default = "default_trigger")]
    pub trigger: InsightTrigger,
    #[serde(default = "default_insight_confidence")]
    pub confidence: f64,
    #[serde(default = "default_validation_state")]
    pub validation_state: ValidationState,
    #[serde(default)]
    pub tests_suggested: Vec<String>,
    #[serde(default)]
    pub expires_at: String,
    #[serde(default)]
    pub sources: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InsightType {
    Hypothesis,
    Strategy,
    Pattern,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InsightTrigger {
    Conflict,
    Failure,
    Synthesis,
    Analogy,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ValidationState {
    Unvalidated,
    Testing,
    Validated,
    Rejected,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Citation {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: CitationType,
    #[serde(default)]
    pub ts: Option<DateTime<Utc>>,
    #[serde(default)]
    pub summary: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CitationType {
    Message,
    ToolResult,
    StatePatch,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct BudgetReport {
    #[serde(default)]
    pub max_tokens: u32, 
    #[serde(default)]
    pub used_tokens_est: u32,
    #[serde(default)]
    pub section_usage: JsonMap,
    #[serde(default)]
    pub degradations: Vec<serde_json::Value>,
    #[serde(default)]
    pub omissions: Vec<serde_json::Value>,
}

fn now() -> DateTime<Utc> {
    Utc::now()
}

fn default_schema_version() -> String {
    "v1".to_string()
}

fn default_tenant_id() -> String {
    "default".to_string()
}

fn default_role_user() -> Role {
    Role::User
}

fn default_fact_status() -> FactStatus {
    FactStatus::Active
}

fn default_scope_level() -> ScopeLevel {
    ScopeLevel::User
}

fn default_confidence() -> f64 {
    0.5
}

fn default_compression_level() -> CompressionLevel {
    CompressionLevel::Raw
}

fn default_trigger() -> InsightTrigger {
    InsightTrigger::Synthesis
}

fn default_validation_state() -> ValidationState {
    ValidationState::Unvalidated
}

fn default_insight_confidence() -> f64 {
    0.3
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_minimal_packet() {
        let packet = MemoryPacket {
            meta: Meta {
                schema_version: "v1".to_string(),
                scope: Scope {
                    tenant_id: "default".to_string(),
                    user_id: "u1".to_string(),
                    agent_id: "a1".to_string(),
                    session_id: "s1".to_string(),
                    run_id: "r1".to_string(),
                },
                generated_at: Utc::now(),
                purpose: Purpose::Planner, 
                task_type: "generic".to_string(),
                cues: JsonMap::new(),
                budget: Budget {
                    max_tokens: 2048,
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
        };

        let json = serde_json::to_string(&packet).unwrap();
        let back: MemoryPacket = serde_json::from_str(&json).unwrap();

        assert_eq!(back.meta.schema_version, "v1");
        assert!(!back.insight.usage_policy.allow_in_responder);
    }
}

//! Persistent, externally evaluated strategy improvement. No implicit model calls.
use memweft_store::{Document, Mutation, Store, StoreError, StoreResult};
use memweft_types::Scope;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::Arc;

fn invalid(message: &str) -> StoreError {
    StoreError::InvalidInput(message.into())
}
fn ns(section: &str) -> Vec<String> {
    vec!["learning".into(), section.into()]
}
fn slot(task: &str, target: &Target) -> String {
    serde_json::to_string(&(task, target)).unwrap()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum Target {
    #[default]
    Task,
    Reflection,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Feedback {
    pub id: String,
    pub task_type: String,
    pub session_id: String,
    pub run_id: String,
    pub success: bool,
    #[serde(default)]
    pub details: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AcceptancePolicy {
    pub min_cases: usize,
    pub min_gain: f64,
    pub max_case_regression: f64,
    pub max_candidate_cost: f64,
    pub max_candidate_latency_ms: f64,
}
impl Default for AcceptancePolicy {
    fn default() -> Self {
        Self {
            min_cases: 3,
            min_gain: 0.05,
            max_case_regression: 0.0,
            max_candidate_cost: 1.0,
            max_candidate_latency_ms: 30_000.0,
        }
    }
}
impl AcceptancePolicy {
    fn validate(&self) -> StoreResult<()> {
        if self.min_cases == 0
            || self.min_cases > 10_000
            || !self.min_gain.is_finite()
            || self.min_gain < 0.0
            || self.min_gain > 1.0
            || !self.max_case_regression.is_finite()
            || !(0.0..=1.0).contains(&self.max_case_regression)
            || !self.max_candidate_cost.is_finite()
            || self.max_candidate_cost < 0.0
            || !self.max_candidate_latency_ms.is_finite()
            || self.max_candidate_latency_ms < 0.0
        {
            return Err(invalid("invalid acceptance policy"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub task_type: String,
    #[serde(default)]
    pub target: Target,
    pub content: String,
    pub proposer_version: String,
    /// User memory keys used to derive the candidate (for invalidation on forget).
    #[serde(default)]
    pub source_keys: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Strategy {
    pub version: String,
    pub parent: Option<String>,
    pub proposal: Proposal,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CaseResult {
    pub case_id: String,
    pub baseline_score: f64,
    pub candidate_score: f64,
    pub candidate_cost: f64,
    pub candidate_latency_ms: f64,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Evaluation {
    pub dataset_version: String,
    pub evaluator_version: String,
    pub cases: Vec<CaseResult>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Evaluating,
    Accepted,
    Rejected,
    Cancelled,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Job {
    pub id: String,
    pub proposal: Proposal,
    pub policy: AcceptancePolicy,
    pub dataset_version: String,
    pub evaluator_version: String,
    pub case_ids: Vec<String>,
    pub baseline: Option<Strategy>,
    pub baseline_revision: u64,
    pub epoch_revision: u64,
    pub status: Status,
    pub reason: String,
    pub evaluation: Option<Evaluation>,
}

/// Generators receive the currently accepted reflection strategy on every round.
/// Implementations may call a model; the default library never does so implicitly.
pub trait Proposer {
    fn propose(
        &self,
        feedback: &[Feedback],
        reflection: Option<&Strategy>,
    ) -> StoreResult<Proposal>;
}
pub trait Evaluator {
    fn evaluate(&self, job: &Job) -> StoreResult<Evaluation>;
}

#[derive(Clone)]
pub struct Learning {
    store: Arc<dyn Store>,
    scope: Scope,
}
impl Learning {
    pub fn new(store: Arc<dyn Store>, scope: Scope) -> Self {
        Self { store, scope }
    }
    fn find(&self, section: &str, key: &str) -> StoreResult<Option<Document>> {
        Ok(self
            .store
            .documents(&self.scope, &ns(section))?
            .into_iter()
            .find(|d| d.namespace == ns(section) && d.key == key))
    }
    pub fn feedback(&self, feedback: Feedback) -> StoreResult<Feedback> {
        if [
            &feedback.id,
            &feedback.task_type,
            &feedback.session_id,
            &feedback.run_id,
        ]
        .iter()
        .any(|v| v.trim().is_empty())
        {
            return Err(invalid(
                "feedback id, task_type, session_id and run_id are required",
            ));
        }
        let value = serde_json::to_value(&feedback)?;
        let mutation = Mutation {
            namespace: ns("feedback"),
            key: feedback.id.clone(),
            value: Some(value.clone()),
            expected_revision: Some(0),
        };
        match self.store.mutate_documents(&self.scope, &[mutation]) {
            Ok(()) => Ok(feedback),
            Err(StoreError::Conflict(_))
                if self
                    .find("feedback", &feedback.id)?
                    .is_some_and(|d| d.value == value) =>
            {
                Ok(feedback)
            }
            Err(e) => Err(e),
        }
    }
    pub fn feedbacks(&self, task: &str) -> StoreResult<Vec<Feedback>> {
        self.store
            .documents(&self.scope, &ns("feedback"))?
            .into_iter()
            .map(|d| serde_json::from_value(d.value).map_err(StoreError::from))
            .filter(|f: &StoreResult<Feedback>| {
                f.as_ref().map(|f| f.task_type == task).unwrap_or(true)
            })
            .collect()
    }
    pub fn active(&self, task: &str, target: &Target) -> StoreResult<Option<Strategy>> {
        self.find("active", &slot(task, target))?
            .and_then(|d| {
                if d.value.is_null() {
                    None
                } else {
                    Some(d.value)
                }
            })
            .map(serde_json::from_value)
            .transpose()
            .map_err(StoreError::from)
    }
    pub fn jobs(&self) -> StoreResult<Vec<Job>> {
        self.store
            .documents(&self.scope, &ns("jobs"))?
            .into_iter()
            .map(|d| serde_json::from_value(d.value).map_err(StoreError::from))
            .collect()
    }
    pub fn get(&self, id: &str) -> StoreResult<Job> {
        serde_json::from_value(self.find("jobs", id)?.ok_or(StoreError::NotFound)?.value)
            .map_err(StoreError::from)
    }
    pub fn start(
        &self,
        id: &str,
        proposal: Proposal,
        policy: AcceptancePolicy,
        dataset_version: &str,
        evaluator_version: &str,
        case_ids: Vec<String>,
    ) -> StoreResult<Job> {
        policy.validate()?;
        if [
            id,
            &proposal.task_type,
            &proposal.content,
            &proposal.proposer_version,
            dataset_version,
            evaluator_version,
        ]
        .iter()
        .any(|s| s.trim().is_empty())
        {
            return Err(invalid(
                "job, proposal and evaluation identifiers must be nonempty",
            ));
        }
        if case_ids.len() < policy.min_cases
            || case_ids.len() > 10_000
            || case_ids.iter().any(|s| s.trim().is_empty())
            || case_ids.iter().collect::<HashSet<_>>().len() != case_ids.len()
        {
            return Err(invalid(
                "evaluation case IDs must be unique and meet min_cases",
            ));
        }
        if let Some(old) = self.find("jobs", id)? {
            let job: Job = serde_json::from_value(old.value)?;
            if job.proposal == proposal
                && job.policy == policy
                && job.dataset_version == dataset_version
                && job.evaluator_version == evaluator_version
                && job.case_ids == case_ids
            {
                return Ok(job);
            }
            return Err(StoreError::Conflict(
                "job id reused with different input".into(),
            ));
        }
        // Read the generation before checking sources. A concurrent forget must
        // either make the source check fail or invalidate this job at adoption.
        let epoch_revision = self
            .find("epoch", "current")?
            .map(|d| d.revision)
            .unwrap_or(0);
        if !proposal.source_keys.is_empty() {
            let facts = self.store.list_facts(
                &self.scope,
                memweft_store::FactFilter {
                    status: Some(vec![memweft_types::FactStatus::Active]),
                    ..Default::default()
                },
            )?;
            if proposal
                .source_keys
                .iter()
                .any(|key| !facts.iter().any(|f| &f.fact_key == key))
            {
                return Err(invalid("proposal references a missing memory key"));
            }
        }
        let active = self.find("active", &slot(&proposal.task_type, &proposal.target))?;
        let baseline_revision = active.as_ref().map(|d| d.revision).unwrap_or(0);
        let baseline = active
            .filter(|d| !d.value.is_null())
            .map(|d| serde_json::from_value(d.value))
            .transpose()?;
        let job = Job {
            id: id.into(),
            proposal,
            policy,
            dataset_version: dataset_version.into(),
            evaluator_version: evaluator_version.into(),
            case_ids,
            baseline,
            baseline_revision,
            epoch_revision,
            status: Status::Evaluating,
            reason: String::new(),
            evaluation: None,
        };
        self.store.mutate_documents(
            &self.scope,
            &[Mutation {
                namespace: ns("jobs"),
                key: id.into(),
                value: Some(serde_json::to_value(&job)?),
                expected_revision: Some(0),
            }],
        )?;
        Ok(job)
    }
    pub fn submit(&self, id: &str, evaluation: Evaluation) -> StoreResult<Job> {
        let doc = self.find("jobs", id)?.ok_or(StoreError::NotFound)?;
        let mut job: Job = serde_json::from_value(doc.value)?;
        if job.status != Status::Evaluating {
            if job.evaluation.as_ref() == Some(&evaluation) {
                return Ok(job);
            }
            return Err(StoreError::Conflict("job is already finished".into()));
        }
        if evaluation.dataset_version != job.dataset_version
            || evaluation.evaluator_version != job.evaluator_version
            || evaluation.cases.len() != job.case_ids.len()
            || evaluation
                .cases
                .iter()
                .map(|c| &c.case_id)
                .collect::<HashSet<_>>()
                != job.case_ids.iter().collect::<HashSet<_>>()
        {
            return Err(invalid(
                "evaluation does not match the job's pinned dataset, evaluator and cases",
            ));
        }
        for c in &evaluation.cases {
            if !c.baseline_score.is_finite()
                || !c.candidate_score.is_finite()
                || !(0.0..=1.0).contains(&c.baseline_score)
                || !(0.0..=1.0).contains(&c.candidate_score)
                || !c.candidate_cost.is_finite()
                || c.candidate_cost < 0.0
                || !c.candidate_latency_ms.is_finite()
                || c.candidate_latency_ms < 0.0
            {
                return Err(invalid(
                    "scores must be finite in [0,1]; cost and latency must be finite and nonnegative",
                ));
            }
        }
        let gain = evaluation
            .cases
            .iter()
            .map(|c| c.candidate_score - c.baseline_score)
            .sum::<f64>()
            / evaluation.cases.len() as f64;
        let cost: f64 = evaluation.cases.iter().map(|c| c.candidate_cost).sum();
        let accepted = gain > 0.0
            && gain >= job.policy.min_gain
            && cost <= job.policy.max_candidate_cost
            && evaluation.cases.iter().all(|c| {
                c.baseline_score - c.candidate_score <= job.policy.max_case_regression
                    && c.candidate_latency_ms <= job.policy.max_candidate_latency_ms
            });
        job.status = if accepted {
            Status::Accepted
        } else {
            Status::Rejected
        };
        job.reason = format!(
            "mean_gain={gain:.6}; total_cost={cost:.6}; policy {}",
            if accepted { "passed" } else { "not met" }
        );
        job.evaluation = Some(evaluation);
        let mut changes = vec![Mutation {
            namespace: ns("jobs"),
            key: id.into(),
            value: Some(serde_json::to_value(&job)?),
            expected_revision: Some(doc.revision),
        }];
        if accepted {
            let mut derived = job.proposal.clone();
            if let Some(baseline) = &job.baseline {
                derived
                    .source_keys
                    .extend(baseline.proposal.source_keys.iter().cloned());
                derived.source_keys.sort();
                derived.source_keys.dedup();
            }
            let strategy = Strategy {
                version: id.into(),
                parent: job.baseline.as_ref().map(|b| b.version.clone()),
                proposal: derived,
            };
            changes.push(Mutation {
                namespace: ns("active"),
                key: slot(&job.proposal.task_type, &job.proposal.target),
                value: Some(serde_json::to_value(&strategy)?),
                expected_revision: Some(job.baseline_revision),
            });
            changes.push(Mutation {
                namespace: ns("versions"),
                key: id.into(),
                value: Some(serde_json::to_value(&strategy)?),
                expected_revision: Some(0),
            });
            changes.push(Mutation {
                namespace: ns("epoch"),
                key: "current".into(),
                value: Some(json!({"last_job": id})),
                expected_revision: Some(job.epoch_revision),
            });
        }
        self.store.mutate_documents(&self.scope, &changes)?;
        Ok(job)
    }
    pub fn finish(&self, id: &str, status: Status, reason: &str) -> StoreResult<Job> {
        if !matches!(status, Status::Failed | Status::Cancelled) {
            return Err(invalid(
                "only failed/cancelled can be set without evaluation",
            ));
        }
        let doc = self.find("jobs", id)?.ok_or(StoreError::NotFound)?;
        let mut job: Job = serde_json::from_value(doc.value)?;
        if job.status == status && job.reason == reason {
            return Ok(job);
        }
        if job.status != Status::Evaluating {
            return Err(StoreError::Conflict("job already finished".into()));
        }
        job.status = status;
        job.reason = reason.into();
        self.store.mutate_documents(
            &self.scope,
            &[Mutation {
                namespace: ns("jobs"),
                key: id.into(),
                value: Some(serde_json::to_value(&job)?),
                expected_revision: Some(doc.revision),
            }],
        )?;
        Ok(job)
    }
    pub fn rollback(
        &self,
        task: &str,
        target: Target,
        version: Option<&str>,
        expected_version: &str,
    ) -> StoreResult<Option<Strategy>> {
        let key = slot(task, &target);
        let current = self.find("active", &key)?.ok_or(StoreError::NotFound)?;
        let active: Strategy = serde_json::from_value(current.value)?;
        if active.version != expected_version {
            return Err(StoreError::Conflict("active version changed".into()));
        }
        let restored: Option<Strategy> = version
            .map(|v| -> StoreResult<Strategy> {
                let s: Strategy = serde_json::from_value(
                    self.find("versions", v)?.ok_or(StoreError::NotFound)?.value,
                )?;
                if s.proposal.task_type != task || s.proposal.target != target {
                    return Err(invalid("version belongs to a different task/target"));
                }
                Ok(s)
            })
            .transpose()?;
        let epoch = self
            .find("epoch", "current")?
            .map(|d| d.revision)
            .unwrap_or(0);
        self.store.mutate_documents(
            &self.scope,
            &[
                Mutation {
                    namespace: ns("active"),
                    key,
                    value: Some(serde_json::to_value(&restored)?),
                    expected_revision: Some(current.revision),
                },
                Mutation {
                    namespace: ns("epoch"),
                    key: "current".into(),
                    value: Some(json!({"rollback": expected_version})),
                    expected_revision: Some(epoch),
                },
                Mutation {
                    namespace: ns("audit"),
                    key: uuid::Uuid::new_v4().to_string(),
                    value: Some(json!({"action":"rollback","from":expected_version,"to":version})),
                    expected_revision: Some(0),
                },
            ],
        )?;
        Ok(restored)
    }
    /// A bounded round with user-supplied Rust implementations. Evaluation is
    /// external to the proposer and acceptance always uses the pinned policy.
    pub fn improve(
        &self,
        id: &str,
        task: &str,
        proposer: &dyn Proposer,
        evaluator: &dyn Evaluator,
        policy: AcceptancePolicy,
        dataset: &str,
        evaluator_version: &str,
        cases: Vec<String>,
    ) -> StoreResult<Job> {
        policy.validate()?;
        let job = if let Some(existing) = self.find("jobs", id)? {
            let job: Job = serde_json::from_value(existing.value)?;
            if job.proposal.task_type != task
                || job.policy != policy
                || job.dataset_version != dataset
                || job.evaluator_version != evaluator_version
                || job.case_ids != cases
            {
                return Err(StoreError::Conflict(
                    "job id reused with different evaluation configuration".into(),
                ));
            }
            job
        } else {
            let reflection = self.active(task, &Target::Reflection)?;
            let proposal = proposer.propose(&self.feedbacks(task)?, reflection.as_ref())?;
            if proposal.task_type != task {
                return Err(invalid("proposer returned a different task_type"));
            }
            self.start(id, proposal, policy, dataset, evaluator_version, cases)?
        };
        if job.status != Status::Evaluating {
            return Ok(job);
        }
        match evaluator.evaluate(&job) {
            Ok(result) => self.submit(id, result),
            Err(e) => {
                self.finish(id, Status::Failed, &e.to_string())?;
                Err(e)
            }
        }
    }
}

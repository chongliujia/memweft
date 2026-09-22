//! Deterministic learning example. Replace the proposer/evaluator with real
//! model and task implementations; the acceptance policy stays in Rust.
use memweft::learning::*;
use memweft::{ContextOptions, Memory};
use memweft_store::StoreResult;
struct ConciseProposer;
impl Proposer for ConciseProposer {
    fn propose(&self, _: &[Feedback], reflection: Option<&Strategy>) -> StoreResult<Proposal> {
        if let Some(strategy) = reflection {
            println!("Using reflection version {}", strategy.version);
        }
        Ok(Proposal {
            task_type: "answer".into(),
            target: Target::Task,
            content: "Answer directly in one sentence when the question is simple.".into(),
            proposer_version: "demo-v1".into(),
            source_pools: vec![],
            source_keys: vec![],
        })
    }
}
struct DemoEvaluator;
impl Evaluator for DemoEvaluator {
    fn evaluate(&self, job: &Job) -> StoreResult<Evaluation> {
        // Synthetic fixture scores demonstrate state transitions, not model quality.
        Ok(Evaluation {
            dataset_version: job.dataset_version.clone(),
            evaluator_version: job.evaluator_version.clone(),
            cases: job
                .case_ids
                .iter()
                .map(|id| CaseResult {
                    case_id: id.clone(),
                    baseline_score: 0.5,
                    candidate_score: 0.8,
                    candidate_cost: 0.0,
                    candidate_latency_ms: 1.0,
                })
                .collect(),
        })
    }
}
fn main() -> StoreResult<()> {
    let memory = Memory::in_memory()?;
    let user = memory.user("alice")?;
    let learning = user.learning();
    learning.feedback(Feedback {
        id: "feedback-1".into(),
        task_type: "answer".into(),
        session_id: "chat".into(),
        run_id: "run-1".into(),
        success: false,
        details: serde_json::json!("Answer was too long"),
    })?;
    let job = learning.improve(
        "strategy-1",
        "answer",
        &ConciseProposer,
        &DemoEvaluator,
        AcceptancePolicy::default(),
        "heldout-demo",
        "fixture-evaluator-v1",
        vec!["a".into(), "b".into(), "c".into()],
    )?;
    println!("{:?}: {}", job.status, job.reason);
    println!(
        "{}",
        user.session("chat")?
            .context(ContextOptions {
                task_type: Some("answer".into()),
                ..Default::default()
            })?
            .text
    );
    Ok(())
}

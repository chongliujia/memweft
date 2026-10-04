mod common;

use memweft::learning::{AcceptancePolicy, CaseResult, Evaluation, Proposal, Target};
use memweft::{Memory, UserMemory, UserScope};
use memweft_store::{SqliteStore, Store};
use memweft_types::{FactStatus, Scope};
use serde_json::json;
use std::sync::{Arc, Barrier};

fn user(memory: &Memory, agent: &str) -> UserMemory {
    memory.scoped(UserScope {
        agent_id: agent.into(),
        memory_config: serde_json::from_value(json!({"read_pools":[{"pool_id":"private","access":"read_write"},{"pool_id":"team","access":"read_write"}],"default_write_pool":"team"})).unwrap(),
        ..UserScope::new("u")
    }).unwrap()
}
fn start(user: &UserMemory, id: &str, source: bool) {
    let proposal: Proposal = serde_json::from_value(
        json!({"task_type":"answer","content":"Use verified source","proposer_version":"fixture",
        "source_pools":if source {json!([{"pool_id":"team","key":"port"}])} else {json!([])}}),
    )
    .unwrap();
    user.learning()
        .start(id, proposal, AcceptancePolicy::default(), "v1", "e1", vec![
            "a".into(),
            "b".into(),
            "c".into(),
        ])
        .unwrap();
}
fn evaluation() -> Evaluation {
    Evaluation {
        dataset_version: "v1".into(),
        evaluator_version: "e1".into(),
        cases: ["a", "b", "c"]
            .into_iter()
            .map(|case_id| CaseResult {
                case_id: case_id.into(),
                baseline_score: 0.0,
                candidate_score: 1.0,
                candidate_cost: 0.0,
                candidate_latency_ms: 1.0,
            })
            .collect(),
    }
}

#[test]
fn file_connections_cannot_restore_invalidated_shared_strategies_during_adoption_and_rollback() {
    let path =
        std::env::temp_dir().join(format!("memweft-source-race-{}.db", uuid::Uuid::new_v4()));
    {
        let a = Memory::open(&path).unwrap();
        let b = Memory::open(&path).unwrap();
        let learner = user(&a, "learner");
        let writer = user(&b, "writer");
        for i in 0..12 {
            writer.remember("port", json!(8000 + i)).unwrap();
            let saved = format!("saved-{i}");
            let independent = format!("independent-{i}");
            let pending = format!("pending-{i}");
            start(&learner, &saved, true);
            learner.learning().submit(&saved, evaluation()).unwrap();
            learner
                .learning()
                .rollback("answer", Target::Task, None, &saved)
                .unwrap();
            start(&learner, &independent, false);
            learner
                .learning()
                .submit(&independent, evaluation())
                .unwrap();
            start(&learner, &pending, true);
            let barrier = Arc::new(Barrier::new(3));
            std::thread::scope(|threads| {
                let gate = barrier.clone();
                let writer = &writer;
                threads.spawn(move || {
                    gate.wait();
                    writer.remember("port", json!(9000 + i)).unwrap();
                });
                let gate = barrier.clone();
                let learner = &learner;
                let pending = &pending;
                threads.spawn(move || {
                    gate.wait();
                    let _ = learner.learning().submit(pending, evaluation());
                });
                barrier.wait();
                let _ =
                    learner
                        .learning()
                        .rollback("answer", Target::Task, Some(&saved), &independent);
            });
            let active = learner.learning().active("answer", &Target::Task).unwrap();
            assert!(active.as_ref().is_none_or(|s| s.version == independent));
            assert!(learner.learning().get(&pending).is_err());
            if let Some(active) = active {
                learner
                    .learning()
                    .rollback("answer", Target::Task, None, &active.version)
                    .unwrap();
            }
        }
    }
    {
        let reopened = Memory::open(&path).unwrap();
        assert!(
            user(&reopened, "learner")
                .learning()
                .active("answer", &Target::Task)
                .unwrap()
                .is_none()
        );
    }
    common::remove_database(path).unwrap();
}

#[test]
fn source_point_lookups_preserve_active_status_duplicates_and_scope() {
    let store = Arc::new(SqliteStore::new_in_memory().unwrap());
    let memory = Memory::from_store(store.clone());
    let owner = memory.user("u").unwrap();
    let scope = Scope {
        tenant_id: "default".into(),
        user_id: "u".into(),
        agent_id: "default".into(),
        session_id: String::new(),
        run_id: String::new(),
    };
    let mut original = owner.remember("port", json!(8000)).unwrap();
    assert!(store.has_active_fact(&scope, "port").unwrap());
    original.status = FactStatus::Deprecated;
    store.upsert_fact(&scope, original.clone()).unwrap();
    assert!(!store.has_active_fact(&scope, "port").unwrap());
    original.fact_id = "duplicate".into();
    original.status = FactStatus::Active;
    store.upsert_fact(&scope, original.clone()).unwrap();
    assert!(store.has_active_fact(&scope, "port").unwrap());
    assert!(
        !store
            .has_active_fact(
                &Scope {
                    agent_id: "other".into(),
                    ..scope.clone()
                },
                "port"
            )
            .unwrap()
    );
    let record = store
        .put_pool_fact(&scope, "team", original, Some(0))
        .unwrap();
    assert_eq!(
        store
            .pool_fact(&scope, "team", "port")
            .unwrap()
            .unwrap()
            .revision,
        record.revision
    );
    assert!(
        store
            .pool_fact(
                &Scope {
                    user_id: "other".into(),
                    ..scope.clone()
                },
                "team",
                "port"
            )
            .unwrap()
            .is_none()
    );
    store
        .forget_pool_fact(&scope, "team", "port", record.revision)
        .unwrap();
    assert!(store.pool_fact(&scope, "team", "port").unwrap().is_none());
}

use memweft::learning::{AcceptancePolicy, CaseResult, Evaluation, Proposal, Status, Target};
use memweft::{ContextOptions, Memory, UserScope};
use memweft_store::{Mutation, SqliteStore, Store};
use serde_json::{Value, json};
use std::sync::Arc;

fn proposal(content: &str) -> Proposal {
    Proposal {
        task_type: "answer".into(),
        target: Target::Task,
        content: content.into(),
        proposer_version: "p1".into(),
        source_keys: vec![],
    }
}
fn cases() -> Vec<String> {
    vec!["a".into(), "b".into(), "c".into()]
}
fn evaluation(score: f64) -> Evaluation {
    Evaluation {
        dataset_version: "heldout-v1".into(),
        evaluator_version: "e1".into(),
        cases: cases()
            .into_iter()
            .map(|case_id| CaseResult {
                case_id,
                baseline_score: 0.5,
                candidate_score: score,
                candidate_cost: 0.01,
                candidate_latency_ms: 10.0,
            })
            .collect(),
    }
}

#[test]
fn persistence_isolation_upsert_and_forget() {
    let path = std::env::temp_dir().join(format!("memweft-{}.db", uuid::Uuid::new_v4()));
    {
        let memory = Memory::open(&path).unwrap();
        let alice = memory.user("alice").unwrap();
        alice.remember("style", json!("long")).unwrap();
        alice.remember("style", json!("brief")).unwrap();
        assert_eq!(alice.memories().unwrap().len(), 1);
        assert!(memory.user("bob").unwrap().memories().unwrap().is_empty());
        let other = memory
            .scoped(UserScope {
                user_id: "alice".into(),
                tenant_id: "other".into(),
                agent_id: "default".into(),
            })
            .unwrap();
        assert!(other.memories().unwrap().is_empty());
        assert!(
            memory
                .scoped(UserScope {
                    user_id: "alice".into(),
                    tenant_id: "default".into(),
                    agent_id: "other".into()
                })
                .unwrap()
                .memories()
                .unwrap()
                .is_empty()
        );
        let chat = alice.session("chat").unwrap();
        let first = chat
            .add_message("user", "hello", Some("event"), Some("run1"))
            .unwrap();
        let retry = chat
            .add_message("user", "hello", Some("event"), Some("run2"))
            .unwrap();
        assert_eq!(first.created_at, retry.created_at);
        assert_eq!(chat.messages().unwrap().len(), 1);
        assert!(
            chat.add_message("user", "changed", Some("event"), None)
                .is_err()
        );
        assert!(
            alice
                .session("other")
                .unwrap()
                .messages()
                .unwrap()
                .is_empty()
        );
    }
    {
        let memory = Memory::open(&path).unwrap();
        let alice = memory.user("alice").unwrap();
        assert_eq!(alice.memories().unwrap()[0].value, "brief");
        let chat = alice.session("chat").unwrap();
        assert!(
            chat.context(ContextOptions::default())
                .unwrap()
                .text
                .contains("hello")
        );
        assert_eq!(chat.clear().unwrap(), 1);
        assert!(alice.forget("style").unwrap());
        assert!(!alice.forget("style").unwrap());
        assert!(
            chat.context(ContextOptions::default())
                .unwrap()
                .text
                .is_empty()
        );
    }
    std::fs::remove_file(path).unwrap();
}

#[test]
fn unicode_budget_and_framework_history_exclusion() {
    let memory = Memory::in_memory().unwrap();
    let user = memory.user("alice").unwrap();
    user.remember("large", json!("记忆".repeat(1000))).unwrap();
    let chat = user.session("chat").unwrap();
    chat.add_message("user", "hello", None, None).unwrap();
    for budget in [0, 1, 10, 100, 10000] {
        let context = chat
            .context(ContextOptions {
                max_tokens: budget,
                ..Default::default()
            })
            .unwrap();
        assert!(context.text.len().div_ceil(4) <= budget as usize);
        assert!(context.report["estimated_tokens"].as_u64().unwrap() <= u64::from(budget));
    }
    let context = chat
        .context(ContextOptions {
            include_messages: false,
            ..Default::default()
        })
        .unwrap();
    assert!(context.messages.is_empty());
    assert!(!context.text.contains("hello"));
}

#[test]
fn learning_accept_reject_rollback_and_idempotence() {
    let memory = Memory::in_memory().unwrap();
    let user = memory.user("alice").unwrap();
    let learning = user.learning();
    let start = |id: &str| {
        learning
            .start(
                id,
                proposal("be precise"),
                AcceptancePolicy::default(),
                "heldout-v1",
                "e1",
                cases(),
            )
            .unwrap()
    };
    start("good");
    let accepted = learning.submit("good", evaluation(0.8)).unwrap();
    assert_eq!(accepted.status, Status::Accepted);
    assert_eq!(learning.submit("good", evaluation(0.8)).unwrap(), accepted);
    assert_eq!(
        learning
            .active("answer", &Target::Task)
            .unwrap()
            .unwrap()
            .version,
        "good"
    );
    let context = user
        .session("s")
        .unwrap()
        .context(ContextOptions {
            task_type: Some("answer".into()),
            ..Default::default()
        })
        .unwrap();
    assert!(context.text.contains("be precise"));
    assert!(
        memory
            .user("bob")
            .unwrap()
            .learning()
            .active("answer", &Target::Task)
            .unwrap()
            .is_none()
    );
    start("bad");
    assert_eq!(
        learning.submit("bad", evaluation(0.4)).unwrap().status,
        Status::Rejected
    );
    assert_eq!(
        learning
            .active("answer", &Target::Task)
            .unwrap()
            .unwrap()
            .version,
        "good"
    );
    start("good2");
    learning.submit("good2", evaluation(0.9)).unwrap();
    assert!(
        learning
            .rollback("answer", Target::Task, Some("good"), "wrong")
            .is_err()
    );
    learning
        .rollback("answer", Target::Task, Some("good"), "good2")
        .unwrap();
    assert_eq!(
        learning
            .active("answer", &Target::Task)
            .unwrap()
            .unwrap()
            .version,
        "good"
    );
    learning
        .rollback("answer", Target::Task, None, "good")
        .unwrap();
    assert!(learning.active("answer", &Target::Task).unwrap().is_none());
}

#[test]
fn rejects_mismatched_duplicate_or_invalid_evidence() {
    let memory = Memory::in_memory().unwrap();
    let learning = memory.user("alice").unwrap().learning();
    learning
        .start(
            "j",
            proposal("candidate"),
            AcceptancePolicy::default(),
            "heldout-v1",
            "e1",
            cases(),
        )
        .unwrap();
    let mut e = evaluation(0.8);
    e.dataset_version = "training".into();
    assert!(learning.submit("j", e).is_err());
    let mut e = evaluation(0.8);
    e.cases[0].case_id = "b".into();
    assert!(learning.submit("j", e).is_err());
    let mut e = evaluation(0.8);
    e.cases[0].candidate_score = f64::NAN;
    assert!(learning.submit("j", e).is_err());
    let mut e = evaluation(0.8);
    e.cases[0].candidate_cost = -1.0;
    assert!(learning.submit("j", e).is_err());
    let mut e = evaluation(0.8);
    e.cases[0].candidate_latency_ms = 1e9;
    assert_eq!(learning.submit("j", e).unwrap().status, Status::Rejected);
    assert!(learning.submit("j", evaluation(0.9)).is_err());
    assert!(learning.active("answer", &Target::Task).unwrap().is_none());
}

#[test]
fn competing_candidates_do_not_overwrite_the_active_version() {
    let memory = Memory::in_memory().unwrap();
    let learning = memory.user("alice").unwrap().learning();
    for id in ["a", "b"] {
        learning
            .start(
                id,
                proposal(id),
                AcceptancePolicy::default(),
                "heldout-v1",
                "e1",
                cases(),
            )
            .unwrap();
    }
    let a = learning.clone();
    let b = learning.clone();
    let barrier = Arc::new(std::sync::Barrier::new(2));
    let b1 = barrier.clone();
    let one = std::thread::spawn(move || {
        b1.wait();
        a.submit("a", evaluation(0.8))
    });
    let two = std::thread::spawn(move || {
        barrier.wait();
        b.submit("b", evaluation(0.8))
    });
    assert_ne!(one.join().unwrap().is_ok(), two.join().unwrap().is_ok());
    assert_eq!(
        learning
            .jobs()
            .unwrap()
            .iter()
            .filter(|j| j.status == Status::Accepted)
            .count(),
        1
    );
}

#[test]
fn cancellation_and_forgetting_invalidate_old_evaluations() {
    let memory = Memory::in_memory().unwrap();
    let user = memory.user("alice").unwrap();
    user.remember("preference", json!("brief")).unwrap();
    let learning = user.learning();
    learning
        .start(
            "cancel",
            proposal("candidate"),
            AcceptancePolicy::default(),
            "heldout-v1",
            "e1",
            cases(),
        )
        .unwrap();
    learning
        .finish("cancel", Status::Cancelled, "stop")
        .unwrap();
    assert!(learning.submit("cancel", evaluation(0.8)).is_err());
    let mut p = proposal("private preference");
    p.source_keys = vec!["preference".into()];
    learning
        .start(
            "derived",
            p,
            AcceptancePolicy::default(),
            "heldout-v1",
            "e1",
            cases(),
        )
        .unwrap();
    learning.submit("derived", evaluation(0.8)).unwrap();
    learning
        .start(
            "pending",
            proposal("pending"),
            AcceptancePolicy::default(),
            "heldout-v1",
            "e1",
            cases(),
        )
        .unwrap();
    user.forget("preference").unwrap();
    assert!(learning.active("answer", &Target::Task).unwrap().is_none());
    assert!(learning.get("derived").is_err());
    assert!(learning.submit("pending", evaluation(0.8)).is_err());
}

#[test]
fn document_transactions_roll_back_all_writes_on_conflict() {
    let store = SqliteStore::new_in_memory().unwrap();
    let scope = memweft_types::Scope {
        tenant_id: "t".into(),
        user_id: "u".into(),
        agent_id: "a".into(),
        session_id: "s".into(),
        run_id: "r".into(),
    };
    let m = |key: &str, expected| Mutation {
        namespace: vec!["test".into()],
        key: key.into(),
        value: Some(json!({"v":1})),
        expected_revision: Some(expected),
    };
    store.mutate_documents(&scope, &[m("existing", 0)]).unwrap();
    assert!(
        store
            .mutate_documents(&scope, &[m("new", 0), m("existing", 0)])
            .is_err()
    );
    assert_eq!(store.documents(&scope, &[]).unwrap().len(), 1);
}

#[test]
fn store_protocol_has_snapshot_reads_filtering_and_namespace_pagination() {
    let memory = Memory::in_memory().unwrap();
    let call = |ops: Value| {
        memory
            .request(json!({"op":"store_batch","scope":{"user_id":"u"},"operations":ops}))
            .unwrap()
    };
    let result = call(json!([
        {"kind":"put","namespace":["a","x"],"key":"1","value":{"score":1}},
        {"kind":"get","namespace":["a","x"],"key":"1"},
        {"kind":"put","namespace":["a","x"],"key":"1","value":{"score":2}},
        {"kind":"put","namespace":["a","y"],"key":"2","value":{"score":3}}
    ]));
    assert!(result[1].is_null());
    let result = call(json!([
        {"kind":"search","prefix":["a"],"filter":{"score":{"$gte":2}},"limit":10,"offset":0},
        {"kind":"list","conditions":[{"match_type":"suffix","path":["x"]}],"max_depth":null,"limit":10,"offset":0}
    ]));
    assert_eq!(result[0].as_array().unwrap().len(), 2);
    assert_eq!(result[1], json!([["a", "x"]]));
}

#[test]
fn shared_language_contract() {
    let memory = Memory::in_memory().unwrap();
    let fixture: Vec<Value> =
        serde_json::from_str(include_str!("../../../tests/contract.json")).unwrap();
    for case in fixture {
        let actual = memory.request(case["request"].clone()).unwrap();
        for (pointer, expected) in case["checks"].as_object().unwrap() {
            assert_eq!(
                actual.pointer(pointer).unwrap(),
                expected,
                "request: {}",
                case["request"]
            );
        }
    }
}

#[test]
fn accepted_reflection_strategy_is_used_by_next_proposer_round() {
    use memweft::learning::{Evaluator, Feedback, Job, Proposer, Strategy};
    use memweft_store::StoreResult;
    struct P;
    impl Proposer for P {
        fn propose(&self, _: &[Feedback], reflection: Option<&Strategy>) -> StoreResult<Proposal> {
            assert_eq!(reflection.unwrap().version, "reflection-v1");
            Ok(proposal("generated with improved reflection"))
        }
    }
    struct E;
    impl Evaluator for E {
        fn evaluate(&self, _: &Job) -> StoreResult<Evaluation> {
            Ok(evaluation(0.9))
        }
    }
    let memory = Memory::in_memory().unwrap();
    let learning = memory.user("alice").unwrap().learning();
    let mut p = proposal("Require a concrete failure and a measurable correction");
    p.target = Target::Reflection;
    learning
        .start(
            "reflection-v1",
            p,
            AcceptancePolicy::default(),
            "heldout-v1",
            "e1",
            cases(),
        )
        .unwrap();
    learning.submit("reflection-v1", evaluation(0.8)).unwrap();
    let job = learning
        .improve(
            "next",
            "answer",
            &P,
            &E,
            AcceptancePolicy::default(),
            "heldout-v1",
            "e1",
            cases(),
        )
        .unwrap();
    assert_eq!(job.status, Status::Accepted);
}

#[test]
fn persisted_jobs_resume_without_regenerating_candidates() {
    use memweft::learning::{Evaluator, Feedback, Job, Proposer, Strategy};
    use memweft_store::StoreResult;
    struct NoProposer;
    impl Proposer for NoProposer {
        fn propose(&self, _: &[Feedback], _: Option<&Strategy>) -> StoreResult<Proposal> {
            panic!("persisted candidate must be reused")
        }
    }
    struct E;
    impl Evaluator for E {
        fn evaluate(&self, _: &Job) -> StoreResult<Evaluation> {
            Ok(evaluation(0.8))
        }
    }
    let path = std::env::temp_dir().join(format!("memweft-job-{}.db", uuid::Uuid::new_v4()));
    {
        let m = Memory::open(&path).unwrap();
        m.user("alice")
            .unwrap()
            .learning()
            .start(
                "resume",
                proposal("candidate"),
                AcceptancePolicy::default(),
                "heldout-v1",
                "e1",
                cases(),
            )
            .unwrap();
    }
    {
        let m = Memory::open(&path).unwrap();
        let learning = m.user("alice").unwrap().learning();
        assert_eq!(
            learning
                .improve(
                    "resume",
                    "answer",
                    &NoProposer,
                    &E,
                    AcceptancePolicy::default(),
                    "heldout-v1",
                    "e1",
                    cases()
                )
                .unwrap()
                .status,
            Status::Accepted
        );
        assert_eq!(
            learning
                .improve(
                    "resume",
                    "answer",
                    &NoProposer,
                    &E,
                    AcceptancePolicy::default(),
                    "heldout-v1",
                    "e1",
                    cases()
                )
                .unwrap()
                .status,
            Status::Accepted
        );
        let mut missing = proposal("missing");
        missing.source_keys = vec!["absent".into()];
        assert!(
            learning
                .start(
                    "invalid",
                    missing,
                    AcceptancePolicy::default(),
                    "heldout-v1",
                    "e1",
                    cases()
                )
                .is_err()
        );
    }
    std::fs::remove_file(path).unwrap();
}

use memweft::learning::{AcceptancePolicy, CaseResult, Evaluation, Proposal, Target};
use memweft::{ContextOptions, Memory, UserMemory, UserScope};
use memweft_store::{Mutation, SqliteStore, Store};
use serde_json::json;
use std::sync::Arc;

fn evidence() -> Evaluation {
    Evaluation {
        dataset_version: "v1".into(),
        evaluator_version: "e1".into(),
        cases: ["a", "b", "c"]
            .into_iter()
            .map(|id| CaseResult {
                case_id: id.into(),
                baseline_score: 0.0,
                candidate_score: 1.0,
                candidate_cost: 0.0,
                candidate_latency_ms: 1.0,
            })
            .collect(),
    }
}

fn start(user: &UserMemory, id: &str, task: &str, sources: &[&str]) {
    user.learning()
        .start(
            id,
            Proposal {
                task_type: task.into(),
                target: Target::Task,
                content: "Use the saved preference".into(),
                proposer_version: "p1".into(),
                source_keys: sources.iter().map(|s| (*s).into()).collect(),
                source_pools: vec![],
            },
            AcceptancePolicy::default(),
            "v1",
            "e1",
            vec!["a".into(), "b".into(), "c".into()],
        )
        .unwrap();
}

#[test]
fn private_updates_invalidate_inherited_sources_pending_jobs_and_saved_versions() {
    let path = std::env::temp_dir().join(format!("memweft-hardening-{}.db", uuid::Uuid::new_v4()));
    {
        let memory = Memory::open(&path).unwrap();
        let alice = memory.user("alice").unwrap();
        let others = [
            memory.user("bob").unwrap(),
            memory
                .scoped(UserScope {
                    user_id: "alice".into(),
                    agent_id: "other".into(),
                    ..UserScope::new("alice")
                })
                .unwrap(),
            memory
                .scoped(UserScope {
                    tenant_id: "other".into(),
                    ..UserScope::new("alice")
                })
                .unwrap(),
        ];
        for user in std::iter::once(&alice).chain(others.iter()) {
            user.remember("style", json!("brief")).unwrap();
            start(user, "v1", "answer", &["style"]);
            user.learning().submit("v1", evidence()).unwrap();
        }
        start(&alice, "v2", "answer", &[]);
        alice.learning().submit("v2", evidence()).unwrap();
        start(&alice, "independent", "other-task", &[]);
        alice.learning().submit("independent", evidence()).unwrap();
        start(&alice, "pending", "answer", &[]);
        alice.remember("style", json!("detailed")).unwrap();
        assert!(
            alice
                .learning()
                .active("answer", &Target::Task)
                .unwrap()
                .is_none()
        );
        for id in ["v1", "v2", "pending"] {
            assert!(alice.learning().get(id).is_err());
        }
        assert!(alice.learning().submit("pending", evidence()).is_err());
        assert!(
            alice
                .learning()
                .active("other-task", &Target::Task)
                .unwrap()
                .is_some()
        );
        for user in others {
            assert!(
                user.learning()
                    .active("answer", &Target::Task)
                    .unwrap()
                    .is_some()
            );
        }
        let context = alice
            .session("s")
            .unwrap()
            .context(ContextOptions {
                task_type: Some("answer".into()),
                ..Default::default()
            })
            .unwrap();
        assert!(context.strategies.is_empty());
        assert_eq!(context.memories[0].value, "detailed");
    }
    {
        let memory = Memory::open(&path).unwrap();
        let alice = memory.user("alice").unwrap();
        assert!(
            alice
                .learning()
                .active("answer", &Target::Task)
                .unwrap()
                .is_none()
        );
        start(&alice, "fresh", "answer", &["style"]);
        alice.learning().submit("fresh", evidence()).unwrap();
        assert!(
            alice
                .learning()
                .rollback("answer", Target::Task, Some("v1"), "fresh")
                .is_err()
        );
        // Reaffirming a private source has the same invalidation semantics as a shared write.
        alice.remember("style", json!("detailed")).unwrap();
        assert!(
            alice
                .learning()
                .active("answer", &Target::Task)
                .unwrap()
                .is_none()
        );
    }
    std::fs::remove_file(path).unwrap();
}

#[test]
fn low_level_private_renames_duplicates_and_failed_writes_preserve_learning_consistency() {
    let store = Arc::new(SqliteStore::new_in_memory().unwrap());
    let memory = Memory::from_store(store.clone());
    let user = memory.user("u").unwrap();
    let scope = memweft_types::Scope {
        tenant_id: "default".into(),
        user_id: "u".into(),
        agent_id: "default".into(),
        session_id: "".into(),
        run_id: "".into(),
    };
    let original = user.remember("style", json!("brief")).unwrap();
    start(&user, "old", "answer", &["style"]);
    user.learning().submit("old", evidence()).unwrap();
    let mut invalid = original.clone();
    invalid.value = json!("should not commit");
    invalid.confidence = f64::NAN; // SQLite rejects NULL in the required confidence column.
    assert!(store.upsert_fact(&scope, invalid).is_err());
    assert_eq!(user.memories().unwrap()[0].value, "brief");
    assert_eq!(
        user.learning()
            .active("answer", &Target::Task)
            .unwrap()
            .unwrap()
            .version,
        "old"
    );
    let mut duplicate = original.clone();
    duplicate.fact_id = "second-id".into();
    store.upsert_fact(&scope, duplicate).unwrap();
    assert!(
        user.learning()
            .active("answer", &Target::Task)
            .unwrap()
            .is_none()
    );
    start(&user, "before-rename", "answer", &["style"]);
    user.learning().submit("before-rename", evidence()).unwrap();
    let mut renamed = original;
    renamed.fact_key = "new-key".into();
    store.upsert_fact(&scope, renamed).unwrap();
    assert!(
        user.learning()
            .active("answer", &Target::Task)
            .unwrap()
            .is_none()
    );
}

#[test]
fn concurrent_private_updates_cannot_republish_old_evaluations_or_rollbacks() {
    for _ in 0..24 {
        let memory = Memory::in_memory().unwrap();
        let user = memory.user("u").unwrap();
        user.remember("style", json!("brief")).unwrap();
        start(&user, "saved", "answer", &["style"]);
        user.learning().submit("saved", evidence()).unwrap();
        user.learning()
            .rollback("answer", Target::Task, None, "saved")
            .unwrap();
        start(&user, "independent", "answer", &[]);
        user.learning().submit("independent", evidence()).unwrap();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let writer = user.clone();
        let gate = barrier.clone();
        let thread = std::thread::spawn(move || {
            gate.wait();
            writer.remember("style", json!("detailed")).unwrap();
        });
        barrier.wait();
        let _ = user
            .learning()
            .rollback("answer", Target::Task, Some("saved"), "independent");
        thread.join().unwrap();
        assert!(
            user.learning()
                .active("answer", &Target::Task)
                .unwrap()
                .is_none_or(|strategy| strategy.version == "independent")
        );

        start(&user, "pending", "answer", &["style"]);
        let writer = user.clone();
        let gate = barrier.clone();
        let thread = std::thread::spawn(move || {
            gate.wait();
            writer.remember("style", json!("new preference")).unwrap();
        });
        barrier.wait();
        let _ = user.learning().submit("pending", evidence());
        thread.join().unwrap();
        assert!(
            user.learning()
                .active("answer", &Target::Task)
                .unwrap()
                .is_none_or(|strategy| strategy.version == "independent")
        );
    }
}

#[test]
fn long_conversation_windows_keep_order_bounded_diagnostics_and_idempotence() {
    let store = Arc::new(SqliteStore::new_in_memory().unwrap());
    let memory = Memory::from_store(store.clone());
    let scope = memweft_types::Scope {
        tenant_id: "default".into(),
        user_id: "u".into(),
        agent_id: "default".into(),
        session_id: "".into(),
        run_id: "".into(),
    };
    let mutations: Vec<_> = (0..10_000)
        .map(|i| Mutation {
            namespace: vec!["messages".into(), "long".into()],
            key: format!("{i:05}"),
            value: Some(json!({"role":"user", "content":format!("message {i}"), "run_id":null})),
            expected_revision: Some(0),
        })
        .collect();
    store.mutate_documents(&scope, &mutations).unwrap();
    let user = memory.user("u").unwrap();
    let chat = user.session("long").unwrap();
    let all = chat.messages().unwrap();
    for window in [0, 1, 10, 9_999, 10_000] {
        let context = chat
            .context(ContextOptions {
                conversation_window: window,
                max_tokens: 1_000_000,
                ..Default::default()
            })
            .unwrap();
        assert_eq!(
            context.messages.iter().map(|d| &d.key).collect::<Vec<_>>(),
            all[all.len() - window..]
                .iter()
                .map(|d| &d.key)
                .collect::<Vec<_>>()
        );
        let omitted = context.report["omissions"].as_array().unwrap();
        assert_eq!(omitted.len(), (10_000 - window).min(64));
        assert_eq!(context.report["omissions_truncated"], 10_000 - window > 64);
        for (entry, expected) in omitted.iter().zip(&all) {
            assert_eq!(entry["id"], expected.key);
            assert_eq!(entry["reason"], "conversation_window");
        }
    }
    let retry = chat
        .add_message("user", "message 9999", Some("09999"), None)
        .unwrap();
    assert_eq!(retry.created_at, all.last().unwrap().created_at);
    assert!(
        chat.add_message("user", "different", Some("09999"), None)
            .is_err()
    );
    assert!(
        user.session("longer")
            .unwrap()
            .context(Default::default())
            .unwrap()
            .messages
            .is_empty()
    );
    let hidden = chat
        .context(ContextOptions {
            include_messages: false,
            ..Default::default()
        })
        .unwrap();
    assert!(hidden.messages.is_empty());
    assert_eq!(hidden.report["omissions"], json!([]));
    let tiny = chat
        .context(ContextOptions {
            max_tokens: 0,
            ..Default::default()
        })
        .unwrap();
    assert!(tiny.messages.is_empty());
    assert!(tiny.text.is_empty());
    assert_eq!(tiny.report["omissions"].as_array().unwrap().len(), 64);
}

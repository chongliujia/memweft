use memweft::{ContextOptions, Memory, UserMemory, UserScope};
use memweft_store::{Mutation, PoolRevision, SqliteStore, Store};
use memweft_types::Scope;
use serde_json::{Value, json};
use std::sync::{Arc, Barrier};

fn config(bindings: &[(&str, &str)], write: Option<&str>, policy: &str) -> Value {
    json!({"read_pools":bindings.iter().map(|(pool,access)| json!({"pool_id":pool,"access":access})).collect::<Vec<_>>(),
        "default_write_pool":write,"conflict_policy":policy})
}
fn scope(agent: &str, config: Value) -> UserScope {
    serde_json::from_value(json!({"user_id":"alice","agent_id":agent,"memory_config":config}))
        .unwrap()
}
fn shared(m: &Memory, agent: &str) -> UserMemory {
    m.scoped(scope(
        agent,
        config(&[("team", "read_write")], Some("team"), "private_first"),
    ))
    .unwrap()
}
fn start(user: &UserMemory, id: &str, sources: Value) {
    user.learning().start(id, serde_json::from_value(json!({"task_type":"answer","content":"Use verified port","proposer_version":"p1","source_pools":sources})).unwrap(),
        Default::default(), "d1", "e1", vec!["a".into(),"b".into(),"c".into()]).unwrap();
}
fn accept(user: &UserMemory, id: &str) {
    user.learning().submit(id, serde_json::from_value(json!({"dataset_version":"d1","evaluator_version":"e1","cases":
        (["a","b","c"].iter().map(|id| json!({"case_id":id,"baseline_score":0.4,"candidate_score":0.8,"candidate_cost":0.01,"candidate_latency_ms":10})).collect::<Vec<_>>())})).unwrap()).unwrap();
}

#[test]
fn shared_visibility_preserves_actor_user_tenant_and_session_boundaries() {
    let m = Memory::in_memory().unwrap();
    let a = shared(&m, "a");
    let b = shared(&m, "b");
    let written = a.remember_in(None, "port", json!(8002), Some(0)).unwrap();
    assert_eq!(written.writer_agent_id, "a");
    assert_eq!(b.memory_records(None).unwrap()[0].revision, Some(1));
    a.session("s")
        .unwrap()
        .add_message("user", "private chat", None, None)
        .unwrap();
    assert!(b.session("s").unwrap().messages().unwrap().is_empty());
    let private_a = m.scoped(scope("a",json!({"read_pools":[{"pool_id":"private","access":"read_write"}],"default_write_pool":"private"}))).unwrap();
    private_a.remember("secret", json!("a only")).unwrap();
    assert_eq!(a.memories().unwrap().len(), 1);
    assert!(
        m.scoped(scope(
            "b",
            serde_json::to_value(memweft::MemoryConfig::default()).unwrap()
        ))
        .unwrap()
        .memories()
        .unwrap()
        .is_empty()
    );
    for (tenant, user) in [("other", "alice"), ("default", "bob")] {
        let mut s = scope(
            "b",
            config(&[("team", "read_write")], Some("team"), "private_first"),
        );
        s.tenant_id = tenant.into();
        s.user_id = user.into();
        assert!(m.scoped(s).unwrap().memories().unwrap().is_empty());
    }
    assert!(
        m.scoped(scope(
            "b",
            config(&[("elsewhere", "read")], None, "private_first")
        ))
        .unwrap()
        .memories()
        .unwrap()
        .is_empty()
    );
}

#[test]
fn mixed_conflicts_provenance_ranking_and_targeted_deletion() {
    let m = Memory::in_memory().unwrap();
    let a = m
        .scoped(scope(
            "a",
            config(
                &[("team", "read_write"), ("private", "read_write")],
                Some("private"),
                "private_first",
            ),
        ))
        .unwrap();
    a.remember_in(Some("team"), "port", json!(8002), None)
        .unwrap();
    a.remember("port", json!(9000)).unwrap();
    a.remember_in(Some("team"), "archive", json!("old"), None)
        .unwrap();
    let c = a
        .session("s")
        .unwrap()
        .context(ContextOptions {
            query: Some("port".into()),
            max_facts: 1,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(c.memories[0].value, 9000);
    assert_eq!(c.report["pools"]["selected"][0]["pool_id"], "private");
    assert_eq!(c.report["pools"]["shadowed"][0]["shadowed_pool"], "team");
    let read_order = m
        .scoped(scope(
            "a",
            config(&[("team", "read"), ("private", "read")], None, "read_order"),
        ))
        .unwrap();
    assert_eq!(read_order.memories().unwrap()[1].value, 8002);
    let strict = m
        .scoped(scope(
            "a",
            config(&[("team", "read"), ("private", "read")], None, "error"),
        ))
        .unwrap();
    assert!(strict.memories().is_err());
    assert_eq!(
        strict.memory_records(Some("private")).unwrap()[0]
            .fact
            .value,
        9000
    );
    assert!(a.forget("port").unwrap()); // only the default private pool
    assert_eq!(a.memories().unwrap()[1].value, 8002); // shared fallback becomes visible
    assert!(a.forget_in(Some("team"), "port", Some(1)).unwrap());
    assert_eq!(strict.memories().unwrap().len(), 1);
    a.remember("archive", json!("old")).unwrap();
    assert_eq!(strict.memories().unwrap().len(), 1); // identical values deduplicate
    assert!(
        a.session("s")
            .unwrap()
            .context(ContextOptions {
                max_tokens: 0,
                ..Default::default()
            })
            .unwrap()
            .report["pools"]["selected"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn bindings_deny_unlisted_and_read_only_writes_and_validate_config() {
    let m = Memory::in_memory().unwrap();
    shared(&m, "a").remember("k", json!(1)).unwrap();
    let reader = m
        .scoped(scope(
            "b",
            config(&[("team", "read")], None, "private_first"),
        ))
        .unwrap();
    assert_eq!(reader.memories().unwrap().len(), 1);
    assert!(reader.remember("k", json!(2)).is_err());
    assert!(
        reader
            .remember_in(Some("team"), "k", json!(2), None)
            .is_err()
    );
    assert!(reader.forget_in(Some("team"), "k", None).is_err());
    assert!(reader.forget("k").is_err());
    assert!(reader.memory_records(Some("private")).is_err());
    assert!(
        reader
            .remember_in(Some("unbound"), "k", json!(2), None)
            .is_err()
    );
    for cfg in [
        config(&[("team", "read")], Some("team"), "private_first"),
        config(
            &[("team", "read"), ("team", "read_write")],
            None,
            "private_first",
        ),
        config(&[(" ", "read")], None, "private_first"),
        config(&[], Some("private"), "private_first"),
    ] {
        assert!(m.scoped(scope("a", cfg)).is_err());
    }
    let explicit = m
        .scoped(scope(
            "a",
            config(&[("team", "read_write")], None, "private_first"),
        ))
        .unwrap();
    assert!(explicit.remember("k", json!(2)).is_err());
    explicit
        .remember_in(Some("team"), "k", json!(2), None)
        .unwrap();
    let private = m.user("alice").unwrap();
    assert!(private.remember_in(None, "k", json!(1), Some(0)).is_err());
    assert!(private.forget_in(None, "k", Some(1)).is_err());
}

#[test]
fn revisions_survive_restart_and_delete_recreate_and_cas_has_one_winner() {
    let path = std::env::temp_dir().join(format!("memweft-pools-{}.db", uuid::Uuid::new_v4()));
    {
        let m = Memory::open(&path).unwrap();
        shared(&m, "a")
            .remember_in(None, "k", json!(1), Some(0))
            .unwrap();
    }
    {
        let m = Memory::open(&path).unwrap();
        assert_eq!(
            shared(&m, "b").memory_records(None).unwrap()[0].revision,
            Some(1)
        );
        let barrier = Arc::new(Barrier::new(2));
        let threads: Vec<_> = ["a", "b"]
            .into_iter()
            .map(|agent| {
                let user = shared(&Memory::open(&path).unwrap(), agent);
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    user.remember_in(None, "k", json!(agent), Some(1)).is_ok()
                })
            })
            .collect();
        assert_eq!(
            threads
                .into_iter()
                .filter_map(|t| t.join().unwrap().then_some(()))
                .count(),
            1
        );
        let a = shared(&m, "a");
        assert!(!a.forget_in(None, "absent", None).unwrap());
        assert!(a.forget_in(None, "k", Some(1)).is_err());
        assert!(a.forget_in(None, "k", Some(2)).unwrap());
        let recreated = a.remember_in(None, "k", json!(4), Some(0)).unwrap();
        assert_eq!(recreated.revision, Some(4));
        assert!(a.remember_in(None, "k", json!(5), Some(2)).is_err());
        assert!(a.remember_in(None, "k", json!(5), Some(0)).is_err());
    }
    std::fs::remove_file(path).unwrap();
}

#[test]
fn shared_updates_and_forget_invalidate_other_agents_and_inherited_learning() {
    let m = Memory::in_memory().unwrap();
    let a = shared(&m, "a");
    let b = shared(&m, "b");
    a.remember("port", json!(8002)).unwrap();
    let refs = json!([{"pool_id":"team","key":"port"}]);
    start(&a, "v1", refs.clone());
    accept(&a, "v1");
    assert!(
        b.learning()
            .active("answer", &Default::default())
            .unwrap()
            .is_none()
    );
    start(&b, "v1", refs.clone());
    accept(&b, "v1");
    start(&b, "v2", json!([]));
    accept(&b, "v2");
    let strategy = b
        .learning()
        .active("answer", &Default::default())
        .unwrap()
        .unwrap();
    assert_eq!(strategy.pool_revisions[0].revision, 1);
    assert_eq!(strategy.proposal.source_pools.len(), 1);
    start(&b, "in-flight", json!([]));
    a.remember("port", json!(9000)).unwrap();
    for user in [&a, &b] {
        assert!(
            user.learning()
                .active("answer", &Default::default())
                .unwrap()
                .is_none()
        );
        assert!(user.learning().jobs().unwrap().is_empty());
    }
    assert!(b.learning().get("in-flight").is_err());
    start(&b, "fresh", refs);
    accept(&b, "fresh");
    assert!(
        b.learning()
            .rollback("answer", Default::default(), Some("v2"), "fresh")
            .is_err()
    );
    // Source-free private strategies survive unrelated pool invalidation.
    let unrelated = shared(&m, "c");
    start(&unrelated, "independent", json!([]));
    accept(&unrelated, "independent");
    a.forget("port").unwrap();
    assert!(
        b.learning()
            .active("answer", &Default::default())
            .unwrap()
            .is_none()
    );
    assert!(
        unrelated
            .learning()
            .active("answer", &Default::default())
            .unwrap()
            .is_some()
    );
}

#[test]
fn learning_requires_explicit_readable_shared_sources_and_hides_unbound_strategies() {
    let m = Memory::in_memory().unwrap();
    let a = shared(&m, "a");
    a.remember("port", json!(8002)).unwrap();
    start(&a, "v1", json!([{"pool_id":"team","key":"port"}]));
    accept(&a, "v1");
    let private = m
        .scoped(scope(
            "a",
            serde_json::to_value(memweft::MemoryConfig::default()).unwrap(),
        ))
        .unwrap();
    assert!(
        private
            .learning()
            .active("answer", &Default::default())
            .unwrap()
            .is_none()
    );
    assert!(
        private
            .session("s")
            .unwrap()
            .context(ContextOptions {
                task_type: Some("answer".into()),
                ..Default::default()
            })
            .unwrap()
            .strategies
            .is_empty()
    );
    for proposal in [
        json!({"source_keys":["port"]}),
        json!({"source_pools":[{"pool_id":"unknown","key":"port"}]}),
        json!({"source_pools":[{"pool_id":"private","key":"port"}]}),
        json!({"source_pools":[{"pool_id":"team","key":"missing"}]}),
    ] {
        let mut p = json!({"task_type":"other","content":"x","proposer_version":"p"});
        p.as_object_mut()
            .unwrap()
            .extend(proposal.as_object().unwrap().clone());
        assert!(
            a.learning()
                .start(
                    "bad",
                    serde_json::from_value(p).unwrap(),
                    Default::default(),
                    "d1",
                    "e1",
                    vec!["a".into(), "b".into(), "c".into()]
                )
                .is_err()
        );
    }
}

#[test]
fn stale_pool_guards_abort_document_transaction_without_partial_writes() {
    let store = Arc::new(SqliteStore::new_in_memory().unwrap());
    let m = Memory::from_store(store.clone());
    let a = shared(&m, "a");
    a.remember("port", json!(8002)).unwrap();
    a.forget("port").unwrap();
    a.remember("port", json!(9000)).unwrap();
    let s = Scope {
        tenant_id: "default".into(),
        user_id: "alice".into(),
        agent_id: "b".into(),
        session_id: "".into(),
        run_id: "".into(),
    };
    let mutation = Mutation {
        namespace: vec!["learning".into(), "versions".into()],
        key: "stale".into(),
        value: Some(json!("must not appear")),
        expected_revision: Some(0),
    };
    assert!(
        store
            .mutate_documents_checked(
                &s,
                &[mutation],
                &[PoolRevision {
                    pool_id: "team".into(),
                    key: "port".into(),
                    revision: 1
                }]
            )
            .is_err()
    );
    assert!(store.documents(&s, &[]).unwrap().is_empty());
}

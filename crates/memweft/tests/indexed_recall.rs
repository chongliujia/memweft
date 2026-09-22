use memweft::{ContextOptions, Memory, UserMemory, UserScope};
use memweft_store::{SqliteStore, Store, lexical_terms};
use memweft_types::{Fact, FactStatus, Scope, ScopeLevel, Validity};
use serde_json::{Value, json};
use std::sync::Arc;

fn scoped(m: &Memory, pools: &[&str], policy: &str, agent: &str) -> UserMemory {
    m.scoped(serde_json::from_value(json!({"user_id":"alice","agent_id":agent,
        "memory_config":{"read_pools":pools.iter().map(|p|json!({"pool_id":p,"access":"read_write"})).collect::<Vec<_>>(),
        "default_write_pool":pools.first(),"conflict_policy":policy}})).unwrap()).unwrap()
}
fn oracle(user: &UserMemory, query: Option<&str>, limit: usize) -> Vec<Fact> {
    let terms = lexical_terms(query.unwrap_or_default());
    let score = |f: &Fact| {
        2 * lexical_terms(&f.fact_key).intersection(&terms).count()
            + lexical_terms(&f.value.to_string())
                .intersection(&terms)
                .count()
    };
    let mut facts: Vec<_> = user
        .memory_records(None)
        .unwrap()
        .into_iter()
        .map(|r| r.fact)
        .collect();
    facts.sort_by(|a, b| {
        score(b)
            .cmp(&score(a))
            .then(a.fact_key.cmp(&b.fact_key))
            .then(a.fact_id.cmp(&b.fact_id))
    });
    facts.truncate(limit);
    facts
}
fn check(user: &UserMemory, query: Option<&str>, k: usize) {
    let c = user
        .session("s")
        .unwrap()
        .context(ContextOptions {
            query: query.map(str::to_string),
            max_facts: k,
            max_tokens: 1_000_000,
            include_messages: false,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(
        serde_json::to_value(&c.memories).unwrap(),
        serde_json::to_value(oracle(user, query, k)).unwrap(),
        "query={query:?}, k={k}"
    );
    assert_eq!(c.report["recall"]["retrieval"], "sqlite_inverted_v1");
    assert!(c.report["recall"]["inspected_facts"].as_u64().unwrap() <= k as u64 + 64);
    assert!(c.report["omissions"].as_array().unwrap().len() <= 64);
    assert!(c.report["pools"]["shadowed"].as_array().unwrap().len() <= 64);
}

#[test]
fn differential_ranking_across_scopes_pool_orders_queries_and_limits() {
    let m = Memory::in_memory().unwrap();
    let writer = scoped(&m, &["private", "team", "org"], "private_first", "a");
    let values = [
        json!("部署端口 port 17443"),
        json!("archive port port port"),
        json!({"host":"API","enabled":true}),
        json!(1234),
        Value::Null,
        json!(["中文", "memory"]),
        json!("其他记录"),
    ];
    for i in 0..180 {
        let key = format!(
            "{}_{i:04}",
            ["port", "部署端口", "archive", "project"][i % 4]
        );
        for (j, pool) in ["private", "team", "org"].iter().enumerate() {
            if (i + j * 7) % 5 != 0 {
                writer
                    .remember_in(
                        Some(pool),
                        &key,
                        values[(i * 13 + j) % values.len()].clone(),
                        None,
                    )
                    .unwrap();
            }
        }
    }
    m.user("foreign")
        .unwrap()
        .remember("port", json!("foreign-canary"))
        .unwrap();
    for pools in [
        &["private"][..],
        &["team"][..],
        &["team", "private", "org"][..],
        &["org", "team", "private"][..],
        &[][..],
    ] {
        for policy in ["private_first", "read_order"] {
            for agent in ["a", "b"] {
                let user = scoped(&m, pools, policy, agent);
                for query in [
                    None,
                    Some(""),
                    Some("PORT"),
                    Some("当前部署端口"),
                    Some("archive port"),
                    Some("中文 memory"),
                    Some("no-match"),
                    Some("true 1234"),
                    Some("\" OR * ' ; --"),
                ] {
                    for k in [0, 1, 10, 30, 200] {
                        check(&user, query, k);
                    }
                }
            }
        }
    }
}

#[test]
fn shadow_diagnostics_keep_selected_order_pool_order_and_cap() {
    let m = Memory::in_memory().unwrap();
    let user = scoped(&m, &["private", "org", "team"], "read_order", "a");
    for i in 0..70 {
        let key = format!("k_{i:03}");
        for pool in ["private", "org", "team"] {
            user.remember_in(Some(pool), &key, json!(pool), None).unwrap();
        }
    }
    // Relevance puts a later key first; diagnostics must follow the selected
    // records rather than silently reverting to lexical key order.
    let c = user.session("s").unwrap().context(ContextOptions {
        query: Some("k_069".into()), max_facts: 70, max_tokens: 100_000,
        include_messages: false, ..Default::default()
    }).unwrap();
    let diagnostics = c.report["pools"]["shadowed"].as_array().unwrap();
    assert_eq!(diagnostics.len(), 64);
    assert_eq!(c.report["pools"]["shadowed_truncated"], true);
    for (index, row) in diagnostics.iter().enumerate() {
        assert_eq!(row["key"], c.memories[index / 2].fact_key);
        assert_eq!(row["selected_pool"], "private");
        assert_eq!(row["shadowed_pool"], if index % 2 == 0 { "org" } else { "team" });
        assert_eq!(row["different_value"], true);
    }
}

#[test]
fn shadowed_high_score_cannot_override_low_score_winner_or_hide_error() {
    let m = Memory::in_memory().unwrap();
    let user = scoped(&m, &["team", "private"], "private_first", "a");
    for i in 0..100 {
        let key = format!("a_{i:03}");
        user.remember_in(Some("team"), &key, json!("port deployment"), None)
            .unwrap();
        user.remember_in(Some("private"), &key, json!("unrelated"), None)
            .unwrap();
    }
    user.remember_in(Some("private"), "z_correct", json!("port"), None)
        .unwrap();
    check(&user, Some("port deployment"), 1);
    assert_eq!(
        oracle(&user, Some("port deployment"), 1)[0].fact_key,
        "z_correct"
    );
    let strict = scoped(&m, &["private", "team"], "error", "a");
    assert!(
        strict
            .session("s")
            .unwrap()
            .context(ContextOptions {
                query: Some("z_correct".into()),
                max_facts: 0,
                ..Default::default()
            })
            .is_err()
    );
    user.forget_in(Some("private"), "a_000", None).unwrap();
    check(&user, Some("port deployment"), 1);
    assert_eq!(
        oracle(&user, Some("port deployment"), 1)[0].fact_key,
        "a_000"
    );
}

fn scope() -> Scope {
    Scope {
        tenant_id: "default".into(),
        user_id: "alice".into(),
        agent_id: "default".into(),
        session_id: String::new(),
        run_id: String::new(),
    }
}
fn fact(id: &str, key: &str, value: Value) -> Fact {
    Fact {
        fact_id: id.into(),
        fact_key: key.into(),
        value,
        status: FactStatus::Active,
        validity: Validity::default(),
        confidence: 1.0,
        sources: vec![],
        scope_level: ScopeLevel::User,
        notes: String::new(),
    }
}
#[test]
fn low_level_writes_duplicates_validity_status_and_deletes_remain_consistent() {
    let store = Arc::new(SqliteStore::new_in_memory().unwrap());
    let m = Memory::from_store(store.clone());
    let user = m.user("alice").unwrap();
    store
        .upsert_fact(&scope(), fact("a", "same", json!("unrelated")))
        .unwrap();
    store
        .upsert_fact(&scope(), fact("b", "same", json!("port")))
        .unwrap();
    store
        .upsert_fact(&scope(), fact("c", "other", json!("port")))
        .unwrap();
    check(&user, Some("port"), 1);
    assert_eq!(oracle(&user, Some("port"), 1)[0].fact_id, "c");
    let mut old = fact("c", "renamed", json!("updated"));
    old.validity.valid_to = Some(chrono::Utc::now() - chrono::Duration::seconds(1));
    store.upsert_fact(&scope(), old.clone()).unwrap();
    check(&user, Some("port"), 10);
    old.validity.valid_to = None;
    old.validity.valid_from = Some(chrono::Utc::now() + chrono::Duration::days(1));
    store.upsert_fact(&scope(), old.clone()).unwrap();
    check(&user, Some("updated"), 10);
    old.validity.valid_from = None;
    store.upsert_fact(&scope(), old.clone()).unwrap();
    check(&user, Some("updated"), 10);
    old.status = FactStatus::Deprecated;
    store.upsert_fact(&scope(), old).unwrap();
    check(&user, Some("updated"), 10);
    user.forget("same").unwrap();
    check(&user, Some("port"), 10);
    assert!(user.memories().unwrap().is_empty());
}

#[test]
fn index_tracks_shared_cas_delete_recreate_and_reopen() {
    let path = std::env::temp_dir().join(format!("recall-{}.db", uuid::Uuid::new_v4()));
    {
        let m = Memory::open(&path).unwrap();
        let user = scoped(&m, &["team"], "private_first", "a");
        let r = user
            .remember_in(None, "key", json!("old port"), Some(0))
            .unwrap();
        assert!(
            user.remember_in(None, "key", json!("failed update"), Some(99))
                .is_err()
        );
        check(&user, Some("old"), 10);
        assert_eq!(oracle(&user, Some("old"), 1)[0].value, "old port");
        user.remember_in(None, "key", json!("new host"), r.revision)
            .unwrap();
        check(&user, Some("host"), 10);
        user.forget_in(None, "key", Some(2)).unwrap();
        check(&user, Some("host"), 10);
        let recreated = user
            .remember_in(None, "key", json!("replacement"), Some(0))
            .unwrap();
        assert_eq!(recreated.revision, Some(4));
    }
    {
        let m = Memory::open(&path).unwrap();
        let reader = scoped(&m, &["team"], "read_order", "b");
        check(&reader, Some("replacement"), 10);
        let foreign = m
            .scoped(UserScope {
                tenant_id: "foreign".into(),
                ..UserScope::new("alice")
            })
            .unwrap();
        check(&foreign, Some("replacement"), 10);
    }
    std::fs::remove_file(path).unwrap();
}

#[test]
fn concurrent_shared_updates_and_queries_use_one_consistent_snapshot() {
    let path = std::env::temp_dir().join(format!("recall-snapshot-{}.db", uuid::Uuid::new_v4()));
    let m = Memory::open(&path).unwrap();
    let user = scoped(&m, &["team"], "private_first", "writer");
    user.remember_in(None, "key", json!("port"), None).unwrap();
    let other = m.clone();
    let task = std::thread::spawn(move || {
        let writer = scoped(&other, &["team"], "private_first", "writer");
        for i in 0..100 {
            writer
                .remember_in(
                    None,
                    "key",
                    json!(if i % 2 == 0 { "host" } else { "port" }),
                    None,
                )
                .unwrap();
        }
    });
    let reader = scoped(&m, &["team"], "private_first", "reader");
    for _ in 0..100 {
        let context = reader
            .session("s")
            .unwrap()
            .context(ContextOptions {
                query: Some("port host".into()),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(context.memories.len(), 1);
        assert_eq!(context.report["recall"]["selected"][0]["score"], 1);
    }
    task.join().unwrap();
    drop(reader);
    drop(user);
    drop(m);
    std::fs::remove_file(path).unwrap();
}

#[test]
fn dense_bounds_preserve_late_winners_ties_and_use_exact_fallback() {
    let m = Memory::in_memory().unwrap();
    let user = m.user("alice").unwrap();
    for i in 0..600 {
        // Key-independent value matches exercise a maximum weight of 1, not 3.
        user.remember(&format!("a_{i:04}"), json!(if i % 2 == 0 {"red common"} else {"blue common"})).unwrap();
    }
    user.remember("z_late", json!("red blue common special")).unwrap();
    let plan = |query: &str| user.session("s").unwrap().context(ContextOptions {
        query: Some(query.into()), max_facts: 10, max_tokens: 1_000_000,
        include_messages: false, ..Default::default()
    }).unwrap();
    // The qualified key prefix reaches the exact bound for a ubiquitous term.
    assert_eq!(plan("common").report["recall"]["ranking_plan"], "bounded_prefix");
    // The sparse term brings the otherwise unseen late high scorer into the set.
    let result = plan("common special");
    assert_eq!(result.memories[0].fact_key, "z_late");
    assert_eq!(result.report["recall"]["ranking_plan"], "bounded_prefix");
    // Disjoint dense lists leave a loose bound: a late overlap must NOT be lost.
    let result = plan("red blue");
    assert_eq!(result.memories[0].fact_key, "z_late");
    assert_eq!(result.report["recall"]["ranking_plan"], "bounded_intersection");
    for query in ["common", "common special", "red blue", "common red blue", "absent"] {
        for k in [0, 1, 10, 64, 193, 194, 600] {
            check(&user, Some(query), k);
        }
    }
    // The fast path's source of maximum weights must track all subsequent writes.
    user.remember("a_0000", json!("removed terms")).unwrap();
    user.forget("z_late").unwrap();
    check(&user, Some("common special red blue"), 10);
}

#[test]
fn dense_multipool_shadowing_and_sparse_boundaries_match_full_resolution() {
    let m = Memory::in_memory().unwrap();
    let writer = scoped(&m, &["private", "team", "org"], "private_first", "a");
    for i in 0..400 {
        let key = format!("key_{i:04}");
        writer.remember_in(Some("team"), &key, json!("red blue common"), None).unwrap();
        writer.remember_in(Some("org"), &key, json!("green common"), None).unwrap();
        if i % 3 != 0 {
            writer.remember_in(Some("private"), &key, json!("unrelated"), None).unwrap();
        }
    }
    // Counts on both sides of the sparse-list threshold; maximum-weight postings
    // late in key order cannot be skipped merely because the prefix scores well.
    for i in 0..65 {
        let key = format!("z_rare_{i:04}");
        writer.remember_in(Some("team"), &key,
            json!(if i < 64 {"rare boundary common"} else {"boundary common"}), None).unwrap();
    }
    for pools in [["private", "team", "org"], ["org", "team", "private"], ["team", "private", "org"]] {
        for policy in ["private_first", "read_order"] {
            for agent in ["a", "b"] {
                let user = scoped(&m, &pools, policy, agent);
                for query in ["common", "common rare", "common boundary", "red green", "red blue common"] {
                    for k in [0, 1, 10, 200] { check(&user, Some(query), k); }
                }
            }
        }
    }
}

#[test]
fn competitive_intersections_preserve_weights_shadowing_and_candidate_cap() {
    let m = Memory::in_memory().unwrap();
    let writer = scoped(&m, &["private", "team", "org"], "private_first", "a");
    for i in 0..700 {
        let key = format!("a_{i:04}");
        writer.remember_in(Some("team"), &key,
            json!(if i % 2 == 0 { "red" } else { "blue" }), None).unwrap();
        if i % 5 == 0 {
            writer.remember_in(Some("private"), &key, json!("unrelated"), None).unwrap();
        }
        writer.remember_in(Some("org"), &key, json!("red blue"), None).unwrap();
    }
    // Scores 2,3,4,5,6 and ties outside the prefix, including key-only matches.
    for (key, value) in [("z_both", "red blue"), ("z_red", "blue"),
        ("z_blue", "blue"), ("z_red_blue", ""), ("z_red_blue_more", "blue"),
        ("z_red_blue_all", "red blue")] {
        writer.remember_in(Some("team"), key, json!(value), None).unwrap();
    }
    // An ineligible high-scoring shared record cannot displace its private winner.
    writer.remember_in(Some("team"), "z_shadowed", json!("red blue"), None).unwrap();
    writer.remember_in(Some("private"), "z_shadowed", json!("none"), None).unwrap();
    for policy in ["private_first", "read_order"] {
        for pools in [["private", "team", "org"], ["org", "team", "private"]] {
            let user = scoped(&m, &pools, policy, "a");
            for query in ["red blue", "blue red", "red none", "red blue none"] {
                for k in [0, 1, 10, 64, 194, 900] { check(&user, Some(query), k); }
            }
        }
    }
    // A large intersection must revert to aggregation, never return a capped set.
    let user = m.user("large-intersection").unwrap();
    for i in 0..2200 {
        user.remember(&format!("z_{i:04}"), json!("red blue")).unwrap();
    }
    for i in 0..200 {
        user.remember(&format!("a_{i:04}"), json!("red")).unwrap();
    }
    check(&user, Some("red blue"), 10);
    let result = user.session("s").unwrap().context(ContextOptions {
        query: Some("red blue".into()), max_facts: 10, include_messages: false,
        ..Default::default()
    }).unwrap();
    assert_eq!(result.report["recall"]["ranking_plan"], "postings_aggregate");
}

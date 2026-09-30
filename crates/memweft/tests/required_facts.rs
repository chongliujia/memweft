use memweft::{ContextOptions, Memory, UserMemory, UserScope};
use memweft_store::{SqliteStore, Store};
use memweft_types::{Fact, FactStatus, Scope, ScopeLevel, Validity};
use serde_json::json;
use std::sync::Arc;

fn context(user: &UserMemory, keys: &[&str], max_facts: usize, max_tokens: u32) -> memweft::Context {
    user.session("s").unwrap().context(ContextOptions {
        query: Some("needle".into()),
        required_fact_keys: keys.iter().map(|key| (*key).into()).collect(),
        max_facts, max_tokens, include_messages: false, ..Default::default()
    }).unwrap()
}

#[test]
fn required_keys_recover_facts_outside_recall_window_in_declared_order() {
    let memory = Memory::in_memory().unwrap();
    let user = memory.user("alice").unwrap();
    for i in 0..100 {
        user.remember(&format!("a_{i:03}"), json!("needle")).unwrap();
    }
    user.remember("z_policy", json!("approval needed")).unwrap();
    user.remember("y_owner", json!("ops")).unwrap();
    assert_eq!(context(&user, &[], 2, 10_000).memories[0].fact_key, "a_000");
    let c = context(&user, &["z_policy", "y_owner", "unknown"], 3, 10_000);
    assert_eq!(c.memories.iter().map(|f|f.fact_key.as_str()).collect::<Vec<_>>(),
        vec!["z_policy", "y_owner", "a_000"]);
    assert_eq!(c.report["requirements"], json!({"requested":["z_policy","y_owner","unknown"],
        "included":["z_policy","y_owner"],"missing":["unknown"],"excluded":[],"complete":false}));
    assert_eq!(c.report["recall"]["candidates_truncated"], true);
    assert!(c.report["recall"]["inspected_facts"].as_u64().unwrap() <= 70);
    let complete = context(&user, &["y_owner", "z_policy"], 2, 10_000);
    assert_eq!(complete.report["requirements"]["included"], json!(["y_owner","z_policy"]));
    assert_eq!(complete.report["requirements"]["complete"], true);
}

#[test]
fn required_keys_still_obey_count_and_budget_and_report_missing_separately() {
    let memory = Memory::in_memory().unwrap();
    let user = memory.user("alice").unwrap();
    user.remember("first", json!("one")).unwrap();
    user.remember("second", json!("two")).unwrap();
    let c = context(&user, &["first", "missing", "second"], 1, 10_000);
    assert_eq!(c.report["requirements"]["included"], json!(["first"]));
    assert_eq!(c.report["requirements"]["missing"], json!(["missing"]));
    assert_eq!(c.report["requirements"]["excluded"], json!(["second"]));
    assert_eq!(c.report["omissions"][0]["reason"], "max_facts");
    let c = context(&user, &["first", "second"], 2, 0);
    assert!(c.text.is_empty());
    assert_eq!(c.report["requirements"]["included"], json!([]));
    assert_eq!(c.report["requirements"]["missing"], json!([]));
    assert_eq!(c.report["requirements"]["excluded"], json!(["first","second"]));
    assert_eq!(c.report["requirements"]["complete"], false);
    let c = context(&user, &["first"], 0, 10_000);
    assert!(c.memories.is_empty());
    assert_eq!(c.report["requirements"]["excluded"], json!(["first"]));
    user.forget("first").unwrap();
    assert_eq!(context(&user, &["first"], 1, 10_000).report["requirements"]["missing"], json!(["first"]));
}

fn pooled(memory: &Memory, policy: &str, pools: &[&str]) -> UserMemory {
    memory.scoped(serde_json::from_value(json!({"user_id":"alice",
        "memory_config":{"read_pools":pools.iter().map(|p|json!({"pool_id":p,"access":"read_write"})).collect::<Vec<_>>(),
        "default_write_pool":pools.first(),"conflict_policy":policy}})).unwrap()).unwrap()
}

#[test]
fn required_keys_preserve_scope_pool_precedence_and_global_conflict_policy() {
    let memory = Memory::in_memory().unwrap();
    let user = pooled(&memory, "private_first", &["team", "private"]);
    user.remember_in(Some("private"), "policy", json!("private"), None).unwrap();
    user.remember_in(Some("team"), "policy", json!("shared"), None).unwrap();
    assert_eq!(context(&user, &["policy"], 1, 10_000).memories[0].value, "private");
    assert_eq!(context(&pooled(&memory, "read_order", &["team","private"]), &["policy"], 1, 10_000).memories[0].value, "shared");
    assert_eq!(context(&pooled(&memory, "private_first", &[]), &["policy"], 1, 10_000).report["requirements"]["missing"], json!(["policy"]));
    for scope in [UserScope::new("bob"), UserScope {tenant_id:"other".into(), ..UserScope::new("alice")},
        UserScope {agent_id:"other".into(), ..UserScope::new("alice")} ] {
        let c = context(&memory.scoped(scope).unwrap(), &["policy"], 1, 10_000);
        assert_eq!(c.report["requirements"]["missing"], json!(["policy"]));
    }
    let strict = pooled(&memory, "error", &["private", "team"]);
    // A requested missing key and max_facts=0 cannot hide an unrelated conflict.
    assert!(strict.session("s").unwrap().context(ContextOptions {
        required_fact_keys:vec!["unrelated".into()], max_facts:0, ..Default::default()
    }).is_err());
    user.remember_in(Some("team"), "policy", json!("private"), None).unwrap();
    assert_eq!(context(&strict, &["policy"], 1, 10_000).report["requirements"]["complete"], true);
}

#[test]
fn required_keys_do_not_revive_invalid_private_records() {
    let store = Arc::new(SqliteStore::new_in_memory().unwrap());
    let memory = Memory::from_store(store.clone());
    let scope = Scope {tenant_id:"default".into(),user_id:"alice".into(),agent_id:"default".into(),session_id:String::new(),run_id:String::new()};
    for (key, status, validity) in [
        ("expired",FactStatus::Active,Validity{valid_to:Some(chrono::Utc::now()-chrono::Duration::days(1)),..Default::default()}),
        ("future",FactStatus::Active,Validity{valid_from:Some(chrono::Utc::now()+chrono::Duration::days(1)),..Default::default()}),
        ("deprecated",FactStatus::Deprecated,Validity::default()),
    ] {
        store.upsert_fact(&scope, Fact {fact_id:key.into(),fact_key:key.into(),value:json!("hidden"),status,validity,
            confidence:1.0,sources:vec![],scope_level:ScopeLevel::User,notes:String::new()}).unwrap();
    }
    let c = context(&memory.user("alice").unwrap(), &["expired","future","deprecated"], 10, 10_000);
    assert!(c.memories.is_empty());
    assert_eq!(c.report["requirements"]["missing"], json!(["expired","future","deprecated"]));
}

#[test]
fn required_keys_validate_size_duplicates_and_key_ids() {
    let memory = Memory::in_memory().unwrap();
    let session = memory.user("alice").unwrap().session("s").unwrap();
    for keys in [vec!["same".into(),"same".into()],vec![" ".into()],vec!["x".repeat(1025)],
        (0..65).map(|i|format!("key{i}")).collect()] {
        assert!(session.context(ContextOptions {required_fact_keys:keys, ..Default::default()}).is_err());
    }
    let keys: Vec<_> = (0..64).map(|i|format!("key{i}")).collect();
    let c = session.context(ContextOptions {required_fact_keys:keys.clone(), ..Default::default()}).unwrap();
    assert_eq!(c.report["requirements"]["missing"], json!(keys));
}

#[test]
fn legacy_store_uses_explicit_full_scan_fallback() {
    let memory = Memory::from_store(Arc::new(memweft_store::InMemoryStore::new()));
    let user = memory.user("alice").unwrap();
    user.remember("a_search", json!("needle")).unwrap();
    user.remember("z_policy", json!("approval needed")).unwrap();
    let c = context(&user, &["z_policy"], 1, 10_000);
    assert_eq!(c.memories[0].fact_key, "z_policy");
    assert_eq!(c.report["recall"]["retrieval"], "full_scan");
    assert_eq!(c.report["requirements"]["complete"], true);
}

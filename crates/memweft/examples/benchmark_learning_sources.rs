//! Offline lifecycle benchmark. Run the identical source against both builds.
//! Each invocation uses a fresh directory; seeding and verification are untimed.
use memweft::learning::{AcceptancePolicy, Learning, Proposal, Target};
use memweft_store::{Mutation, PoolRef, SqliteStore, Store, StoreResult};
use memweft_types::{Fact, FactStatus, Scope, ScopeLevel, Validity};
use serde_json::{Value, json};
use std::{path::Path, sync::Arc, time::Instant};

fn scope() -> Scope {
    Scope { tenant_id: "bench".into(), user_id: "u".into(), agent_id: "a".into(),
        session_id: String::new(), run_id: String::new() }
}
fn fact(key: &str, value: usize) -> Fact {
    Fact { fact_id: key.into(), fact_key: key.into(), value: json!(value),
        status: FactStatus::Active, validity: Validity::default(), confidence: 1.0,
        sources: vec![], scope_level: ScopeLevel::User, notes: String::new() }
}
fn doc(key: String, value: Value) -> Mutation {
    Mutation { namespace: vec!["learning".into(), "active".into()], key,
        value: Some(value), expected_revision: None }
}
fn samples(mut action: impl FnMut(usize) -> StoreResult<()>, repeats: usize) -> StoreResult<Value> {
    let mut elapsed = vec![];
    for i in 0..=repeats {
        let start = Instant::now();
        action(i)?;
        let ms = start.elapsed().as_secs_f64() * 1000.0;
        if i > 0 { elapsed.push(ms); }
    }
    let mut sorted = elapsed.clone();
    sorted.sort_by(f64::total_cmp);
    let n = sorted.len();
    let median = if n % 2 == 0 { (sorted[n/2-1] + sorted[n/2])/2.0 } else { sorted[n/2] };
    Ok(json!({"p50_ms":median,"p95_ms":sorted[(n*95).div_ceil(100)-1],"samples_ms":elapsed}))
}
fn run(path: &Path, count: usize, repeats: usize, fanout: usize) -> StoreResult<Value> {
    let store = Arc::new(SqliteStore::new(path)?);
    let scope = scope();
    for i in 0..count {
        let key = format!("fact-{i:08}");
        store.upsert_fact(&scope, fact(&key, i))?;
        store.put_pool_fact(&scope, "team", fact(&key, i), Some(0))?;
    }
    let learning = Learning::new(store.clone(), scope.clone())
        .with_memory_pools(vec!["private".into(), "team".into()]);
    let mut timings = serde_json::Map::new();
    for shared in [false, true] {
        let label = if shared { "shared_start" } else { "private_start" };
        timings.insert(label.into(), samples(|i| {
            learning.start(&format!("{label}-{i}"), Proposal {
                task_type: label.into(), target: Target::Task, content: "Use current source".into(),
                proposer_version: "fixture".into(),
                source_keys: if shared { vec![] } else { vec!["fact-00000000".into()] },
                source_pools: if shared { vec![PoolRef {pool_id:"team".into(), key:"fact-00000000".into()}] } else { vec![] },
            }, AcceptancePolicy::default(), "fixture-v1", "deterministic", vec!["a".into(),"b".into(),"c".into()])?;
            Ok(())
        }, repeats)?);
    }
    // All background documents declare unrelated sources. Their count should
    // not determine the number of payloads decoded during target invalidation.
    for base in (0..count).step_by(500) {
        let changes: Vec<_> = (base..(base+500).min(count)).map(|i| doc(format!("background-{i:08}"),
            json!({"source_keys":["fact-00000001"],"source_pools":[{"pool_id":"team","key":"fact-00000001"}],"payload":"x".repeat(128)}))).collect();
        store.mutate_documents(&scope, &changes)?;
    }
    timings.insert("document_insert".into(), samples(|i| store.mutate_documents(&scope, &[doc(format!("insert-{i}"),
        json!({"source_keys":["fact-00000001"],"source_pools":[{"pool_id":"team","key":"fact-00000001"}]}))]), repeats)?);
    for shared in [false, true] {
        let label = if shared { "shared_update" } else { "private_update" };
        let mut elapsed = vec![];
        for i in 0..=repeats {
            // Reset affected records outside the measured source update.
            let changes: Vec<_> = (0..fanout).map(|n| doc(format!("dependent-{n:08}"), if shared {
                json!({"source_pools":[{"pool_id":"team","key":"fact-00000000"}]})
            } else { json!({"source_keys":["fact-00000000"]}) })).collect();
            store.mutate_documents(&scope, &changes)?;
            let start = Instant::now();
            if shared { store.put_pool_fact(&scope,"team",fact("fact-00000000", i+count),None)?; }
            else { store.upsert_fact(&scope,fact("fact-00000000", i+count))?; }
            let ms = start.elapsed().as_secs_f64()*1000.0;
            if i > 0 { elapsed.push(ms); }
            for n in 0..fanout {
                assert!(store.document(&scope, &["learning".into(),"active".into()], &format!("dependent-{n:08}"))?.unwrap().value.is_null());
            }
            assert!(!store.document(&scope, &["learning".into(),"active".into()], "background-00000000")?.unwrap().value.is_null());
        }
        let mut sorted = elapsed.clone(); sorted.sort_by(f64::total_cmp);
        let n = sorted.len();
        timings.insert(label.into(),json!({"p50_ms":if n%2==0 {(sorted[n/2-1]+sorted[n/2])/2.0} else {sorted[n/2]},
            "p95_ms":sorted[(n*95).div_ceil(100)-1],"samples_ms":elapsed}));
    }
    let background = store.documents(&scope, &["learning".into(),"active".into()])?.into_iter()
        .filter(|d| d.key.starts_with("background-") && !d.value.is_null()).count();
    assert_eq!(background,count);
    assert!(learning.jobs()?.is_empty());
    Ok(json!({"facts_per_pool":count,"unrelated_learning_documents":count,"affected_documents":fanout,
        "repeats":repeats,"verification":{"background_preserved":background,"dependent_values_null":fanout,"pending_jobs_removed":true},"timings":timings}))
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len()!=5 { return Err("usage: benchmark_learning_sources NEW_DIRECTORY COUNT REPEATS FANOUT".into()); }
    let directory=Path::new(&args[1]);
    std::fs::create_dir(directory)?;
    let count=args[2].parse::<usize>()?;
    let repeats=args[3].parse::<usize>()?;
    let fanout=args[4].parse::<usize>()?;
    if count<2 || repeats<2 || fanout==0 { return Err("count/repeats >=2 and fanout >=1 required".into()); }
    let result=run(&directory.join("memory.db"),count,repeats,fanout)?;
    let output=serde_json::to_string_pretty(&result)?;
    std::fs::write(directory.join("result.json"),output+"\n")?;
    println!("{}", json!({"directory":directory,"count":count,"timings":result["timings"],"verified":true}));
    Ok(())
}

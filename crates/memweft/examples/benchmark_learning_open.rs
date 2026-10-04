//! Open an existing fixture to measure startup/migration, without modifying facts.
use memweft::Memory;
use std::time::Instant;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = std::env::args()
        .nth(1)
        .ok_or("usage: benchmark_learning_open DATABASE")?;
    if !std::path::Path::new(&path).is_file() {
        return Err("existing fixture required".into());
    }
    let start = Instant::now();
    let memory = Memory::open(path)?;
    println!(
        "{}",
        serde_json::json!({"open_ms":start.elapsed().as_secs_f64()*1000.0})
    );
    drop(memory);
    Ok(())
}

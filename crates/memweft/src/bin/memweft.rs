use memweft::{ContextOptions, Memory};
use serde_json::{Value, json};

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args.iter().any(|a| a == "--help" || a == "-h") {
        println!(
            "MemWeft\n\nUsage: memweft [--db PATH] COMMAND\n\n  remember USER KEY TEXT\n  memories USER\n  forget USER KEY\n  context USER SESSION\n  request FILE.json\n\nThe default database is data/memweft.db. No model calls are implicit."
        );
        return Ok(());
    }
    let path = if args.first().map(String::as_str) == Some("--db") {
        if args.len() < 3 {
            return Err("--db requires a path and a command".into());
        }
        args.remove(0);
        args.remove(0)
    } else {
        "data/memweft.db".into()
    };
    let memory = Memory::open(path)?;
    let result: Value = match args.as_slice() {
        [op, user, key, text] if op == "remember" => {
            serde_json::to_value(memory.user(user)?.remember(key, json!(text))?)?
        }
        [op, user] if op == "memories" => serde_json::to_value(memory.user(user)?.memories()?)?,
        [op, user, key] if op == "forget" => json!(memory.user(user)?.forget(key)?),
        [op, user, session] if op == "context" => serde_json::to_value(
            memory
                .user(user)?
                .session(session)?
                .context(ContextOptions::default())?,
        )?,
        [op, file] if op == "request" => {
            memory.request(serde_json::from_str(&std::fs::read_to_string(file)?)?)?
        }
        _ => return Err("invalid arguments; run memweft --help".into()),
    };
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

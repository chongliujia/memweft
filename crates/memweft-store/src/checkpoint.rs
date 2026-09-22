//! Bounded maintenance notifications; source writes still commit before returning.
use crate::{StoreError, StoreResult};
use rusqlite::Connection;
use serde::Serialize;
use std::fs::{File, OpenOptions, TryLockError};
use std::path::PathBuf;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc::{self, SyncSender},
};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

#[derive(Clone, Default, Serialize)]
pub(crate) struct Progress {
    pub passive_runs: u64,
    pub reclaim_attempts: u64,
    pub reclaims: u64,
    pub busy_runs: u64,
    pub log_frames: Option<i64>,
    pub checkpointed_frames: Option<i64>,
    pub observed_wal_bytes: u64,
    pub observed_peak_wal_bytes: u64,
    pub last_duration_ms: f64,
    pub last_error: Option<String>,
    pub sampled_at_ms: i64,
    pub coordinator_role: &'static str,
    pub leadership_acquisitions: u64,
    pub coordination_waits: u64,
    pub reader_deferred_runs: u64,
    pub backoff_deferred_runs: u64,
    pub reclaim_busy_streak: u32,
    pub next_reclaim_after_ms: u64,
}

// Keep this sidecar in place: unlinking a locked file could create two lock
// domains. Kernel locks are released on close/process death, without a TTL.
struct Coordinator {
    file: Option<File>,
    leader: bool,
}

impl Coordinator {
    fn open(conn: &Connection) -> StoreResult<Self> {
        // rusqlite::path returns None for a non-UTF-8 filename on Unix. Do
        // not silently treat a real file with an unknown name as in-memory,
        // which would bypass the coordination lock and WAL size checks.
        let name = conn.path().ok_or_else(|| {
            StoreError::InvalidInput(
                "background maintenance requires a UTF-8 database filename".into(),
            )
        })?;
        let file = Some(name)
            .filter(|p| !p.is_empty())
            .map(|path| {
                let path = std::fs::canonicalize(path)?;
                let mut sidecar = path.into_os_string();
                sidecar.push(".memweft-maintenance");
                OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create(true)
                    .truncate(false)
                    .open(sidecar)
            })
            .transpose()
            .map_err(|e| StoreError::Storage(format!("maintenance coordination: {e}")))?;
        Ok(Self {
            file,
            leader: false,
        })
    }

    fn elect(&mut self, state: &mut Progress) -> StoreResult<bool> {
        if self.leader {
            return Ok(true);
        }
        if let Some(file) = &self.file {
            match file.try_lock() {
                Ok(()) => (),
                Err(TryLockError::WouldBlock) => {
                    state.coordinator_role = "follower";
                    state.coordination_waits += 1;
                    return Ok(false);
                }
                Err(TryLockError::Error(e)) => {
                    return Err(StoreError::Storage(format!("maintenance lock: {e}")));
                }
            }
        }
        self.leader = true;
        state.coordinator_role = if self.file.is_some() {
            "leader"
        } else {
            "standalone"
        };
        state.leadership_acquisitions += 1;
        Ok(true)
    }
}

const RECLAIM_RETRY_BASE: Duration = Duration::from_secs(5);
const RECLAIM_RETRY_MAX: Duration = Duration::from_secs(30);
const RECLAIM_COOLDOWN: Duration = Duration::from_secs(30);

#[derive(Default)]
struct ReclaimSchedule {
    next: Option<Instant>,
    busy_streak: u32,
}

impl ReclaimSchedule {
    fn ready(&self, now: Instant) -> bool {
        self.next.is_none_or(|next| now >= next)
    }

    fn completed(&mut self, now: Instant, busy: bool) {
        let delay = if busy {
            self.busy_streak = self.busy_streak.saturating_add(1);
            (RECLAIM_RETRY_BASE * (1 << (self.busy_streak - 1).min(3))).min(RECLAIM_RETRY_MAX)
        } else {
            self.busy_streak = 0;
            RECLAIM_COOLDOWN
        };
        self.next = Some(now + delay);
    }

    fn publish(&self, state: &mut Progress) {
        state.reclaim_busy_streak = self.busy_streak;
        state.next_reclaim_after_ms = self.next.map_or(0, |t| {
            t.saturating_duration_since(Instant::now()).as_millis() as u64
        });
    }
}

pub(crate) struct Checkpointer {
    sender: Option<SyncSender<()>>,
    worker: Option<JoinHandle<()>>,
    pub failed: Arc<AtomicBool>,
    progress: Arc<Mutex<Progress>>,
    interval: Duration,
    threshold: Option<u64>,
}

fn run(conn: &Connection, mode: &str) -> rusqlite::Result<(i64, i64, i64)> {
    conn.query_row(mode, [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
}

fn wal_size(wal: Option<&PathBuf>) -> StoreResult<u64> {
    match wal.map(std::fs::metadata) {
        None => Ok(0),
        Some(Ok(meta)) => Ok(meta.len()),
        Some(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => Ok(0),
        Some(Err(e)) => Err(StoreError::Storage(format!("WAL metadata: {e}"))),
    }
}

fn cycle(
    conn: &Connection,
    wal: Option<&PathBuf>,
    threshold: Option<u64>,
    state: &mut Progress,
    schedule: &mut ReclaimSchedule,
) -> StoreResult<bool> {
    let start = Instant::now();
    state.passive_runs += 1;
    let (mut busy, mut log, mut copied) = run(conn, "PRAGMA wal_checkpoint(PASSIVE)")?;
    let size = wal_size(wal)?;
    state.observed_peak_wal_bytes = state.observed_peak_wal_bytes.max(size);
    if threshold.is_some_and(|limit| size >= limit) {
        if busy != 0 || log < 0 || log > copied {
            // PASSIVE already proves we cannot reclaim this snapshot safely.
            // Keep copying at the normal interval without taking TRUNCATE's
            // writer lock, then reconsider when readers make progress.
            state.reader_deferred_runs += 1;
        } else if !schedule.ready(Instant::now()) {
            state.backoff_deferred_runs += 1;
        } else {
            state.reclaim_attempts += 1;
            // The connection has a zero busy timeout: readers/writers may defer this
            // attempt, never get evicted. SQLite itself owns reset/truncation locks.
            (busy, log, copied) = run(conn, "PRAGMA wal_checkpoint(TRUNCATE)")?;
            if busy == 0 {
                state.reclaims += 1;
            }
            schedule.completed(Instant::now(), busy != 0);
        }
    }
    state.busy_runs += u64::from(busy != 0 || log > copied);
    state.log_frames = (log >= 0).then_some(log);
    state.checkpointed_frames = (copied >= 0).then_some(copied);
    state.observed_wal_bytes = wal_size(wal)?;
    state.last_duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    state.sampled_at_ms = chrono::Utc::now().timestamp_millis();
    schedule.publish(state);
    Ok(busy != 0 || log > copied || threshold.is_some_and(|n| state.observed_wal_bytes >= n))
}

impl Checkpointer {
    pub fn start(
        conn: Connection,
        interval: Duration,
        threshold: Option<u64>,
    ) -> StoreResult<Self> {
        conn.busy_timeout(Duration::ZERO)?;
        let mut coordinator = Coordinator::open(&conn)?;
        let wal = conn
            .path()
            .filter(|p| !p.is_empty())
            .map(|p| PathBuf::from(format!("{p}-wal")));
        let (sender, receiver) = mpsc::sync_channel(1);
        let failed = Arc::new(AtomicBool::new(false));
        let progress = Arc::new(Mutex::new(Progress {
            coordinator_role: "waiting",
            ..Default::default()
        }));
        let status = failed.clone();
        let published = progress.clone();
        let worker = std::thread::Builder::new().name("memweft-checkpoint".into()).spawn(move || {
            struct FailureGuard(Arc<AtomicBool>);
            impl Drop for FailureGuard {
                fn drop(&mut self) { self.0.store(true, Ordering::Release); }
            }
            let _guard = FailureGuard(status);
            let mut pending = true;
            let mut next = Instant::now() + interval;
            let mut state = Progress::default();
            let mut schedule = ReclaimSchedule::default();
            let mut data_version = None;
            loop {
                if Instant::now() >= next {
                    let result = (|| -> StoreResult<()> {
                        if coordinator.elect(&mut state)? {
                            // The leader can be a read-only caller. Detect commits
                            // from other instances/processes on this connection;
                            // their bounded in-process notifications cannot reach us.
                            let version: i64 = conn.query_row("PRAGMA data_version", [], |r|r.get(0))?;
                            pending |= data_version != Some(version);
                            data_version = Some(version);
                            if pending {
                                pending = cycle(&conn, wal.as_ref(), threshold, &mut state, &mut schedule)?;
                            }
                        }
                        Ok(())
                    })();
                    if let Err(error) = &result { state.last_error = Some(error.to_string()); }
                    // Never hold the publication mutex across filesystem I/O.
                    *published.lock().unwrap_or_else(|e|e.into_inner()) = state.clone();
                    if let Err(error) = result {
                        tracing::error!(%error, "background checkpoint failed; restoring automatic checkpoints on subsequent operations");
                        break;
                    }
                    next = Instant::now() + interval;
                }
                match receiver.recv_timeout(next.saturating_duration_since(Instant::now())) {
                    Ok(()) => pending = true,
                    Err(mpsc::RecvTimeoutError::Timeout) => (),
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        if coordinator.leader {
                            // Do not bypass backoff/cooldown or elect a new
                            // leader just to truncate during shutdown.
                            if let Err(error) = cycle(&conn, wal.as_ref(), None, &mut state, &mut schedule) {
                                tracing::error!(%error, "final checkpoint failed; committed data remains in WAL");
                            }
                        }
                        break;
                    }
                }
            }
        }).map_err(|e| StoreError::Storage(e.to_string()))?;
        Ok(Self {
            sender: Some(sender),
            worker: Some(worker),
            failed,
            progress,
            interval,
            threshold,
        })
    }

    pub fn status(&self) -> serde_json::Value {
        serde_json::json!({
            "mode": if self.failed.load(Ordering::Acquire) { "automatic_fallback" } else { "background" },
            "interval_ms": self.interval.as_millis() as u64,
            "wal_reclaim_threshold_bytes": self.threshold,
            "reclaim_busy_timeout_ms": 0,
            "coordination": "database_file_lock",
            "reclaim_retry_base_ms": RECLAIM_RETRY_BASE.as_millis() as u64,
            "reclaim_retry_max_ms": RECLAIM_RETRY_MAX.as_millis() as u64,
            "reclaim_cooldown_ms": RECLAIM_COOLDOWN.as_millis() as u64,
            "progress": self.progress.lock().unwrap_or_else(|e|e.into_inner()).clone(),
        })
    }

    pub fn notify(&self) {
        if let Some(sender) = &self.sender {
            if matches!(
                sender.try_send(()),
                Err(mpsc::TrySendError::Disconnected(_))
            ) {
                self.failed.store(true, Ordering::Release);
            }
        }
    }
}

impl Drop for Checkpointer {
    fn drop(&mut self) {
        self.sender.take();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn unknown_database_filename_cannot_bypass_coordination() {
        use std::os::unix::ffi::OsStringExt;
        let mut name = format!(
            "memweft-path-{}-{}-",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap()
        )
        .into_bytes();
        name.push(0xff);
        let path = std::env::temp_dir().join(std::ffi::OsString::from_vec(name));
        let conn = Connection::open(&path).unwrap();
        assert!(conn.path().is_none());
        let result = Checkpointer::start(conn, Duration::from_millis(100), None);
        assert!(matches!(result, Err(StoreError::InvalidInput(_))));
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn busy_retry_is_bounded_and_success_keeps_a_cooldown() {
        let mut schedule = ReclaimSchedule::default();
        let mut now = Instant::now();
        assert!(schedule.ready(now));
        for delay in [5, 10, 20, 30, 30, 30] {
            schedule.completed(now, true);
            let deadline = now + Duration::from_secs(delay);
            assert!(!schedule.ready(deadline - Duration::from_millis(1)));
            assert!(schedule.ready(deadline));
            now = deadline;
        }
        schedule.completed(now, false);
        assert!(!schedule.ready(now + Duration::from_secs(29)));
        assert!(schedule.ready(now + Duration::from_secs(30)));
        assert_eq!(schedule.busy_streak, 0);
        schedule.completed(now + Duration::from_secs(30), true);
        assert!(schedule.ready(now + Duration::from_secs(35)));
    }

    #[test]
    fn one_leader_detects_other_connections_writes_and_follower_takes_over() {
        let path = std::env::temp_dir().join(format!(
            "memweft-coordinator-{}-{}.db",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap()
        ));
        let writer = Connection::open(&path).unwrap();
        writer
            .execute_batch(
                "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE t(x);",
            )
            .unwrap();
        let first = Checkpointer::start(
            Connection::open(&path).unwrap(),
            Duration::from_millis(100),
            None,
        )
        .unwrap();
        let wait = |worker: &Checkpointer, predicate: &dyn Fn(serde_json::Value) -> bool| {
            let until = Instant::now() + Duration::from_secs(5);
            while Instant::now() < until {
                if predicate(worker.status()) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            panic!("maintenance did not progress: {}", worker.status());
        };
        wait(&first, &|s| s["progress"]["coordinator_role"] == "leader");
        let second = Checkpointer::start(
            Connection::open(&path).unwrap(),
            Duration::from_millis(100),
            None,
        )
        .unwrap();
        wait(&second, &|s| {
            s["progress"]["coordinator_role"] == "follower"
        });
        assert_eq!(second.status()["progress"]["passive_runs"], 0);
        let previous = first.status()["progress"]["passive_runs"].as_u64().unwrap();
        // No notify(): the leader has never received an application write.
        writer.execute("INSERT INTO t VALUES(1)", []).unwrap();
        wait(&first, &|s| {
            s["progress"]["passive_runs"].as_u64().unwrap() > previous
        });
        assert_eq!(second.status()["progress"]["passive_runs"], 0);
        drop(first);
        wait(&second, &|s| s["progress"]["coordinator_role"] == "leader");
        assert_eq!(second.status()["progress"]["leadership_acquisitions"], 1);
        drop(second);
        drop(writer);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_file(format!("{}.memweft-maintenance", path.display())).unwrap();
    }

    #[test]
    fn pinned_snapshot_defers_reclaim_then_recovers_without_another_write() {
        let path = std::env::temp_dir().join(format!(
            "memweft-reclaim-{}-{}.db",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap()
        ));
        let writer = Connection::open(&path).unwrap();
        writer
            .execute_batch(
                "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
            CREATE TABLE t(x); INSERT INTO t VALUES(0); PRAGMA wal_checkpoint(TRUNCATE);",
            )
            .unwrap();
        let reader = Connection::open(&path).unwrap();
        reader.execute_batch("BEGIN; SELECT * FROM t").unwrap();
        writer
            .execute("INSERT INTO t VALUES(zeroblob(131072))", [])
            .unwrap();
        let worker = Checkpointer::start(
            Connection::open(&path).unwrap(),
            Duration::from_millis(100),
            Some(65536),
        )
        .unwrap();
        let wait = |predicate: &dyn Fn(serde_json::Value) -> bool| {
            let until = Instant::now() + Duration::from_secs(5);
            while Instant::now() < until {
                if predicate(worker.status()) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            panic!("checkpoint did not progress: {}", worker.status());
        };
        wait(&|s| s["progress"]["busy_runs"].as_u64().unwrap() > 0);
        assert_eq!(worker.status()["mode"], "background");
        assert_eq!(
            reader
                .query_row("SELECT count(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        reader.execute_batch("COMMIT").unwrap();
        // No notify/write: an earlier busy result must keep the work pending.
        wait(&|s| s["progress"]["reclaims"].as_u64().unwrap() > 0);
        assert_eq!(worker.status()["progress"]["observed_wal_bytes"], 0);
        assert_eq!(
            reader
                .query_row("SELECT count(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            2
        );
        drop(worker);
        drop(reader);
        drop(writer);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_file(format!("{}.memweft-maintenance", path.display())).unwrap();
    }

    #[test]
    fn failed_worker_is_reported_and_shutdown_does_not_wait_for_interval() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE test(x); BEGIN; INSERT INTO test VALUES(1)")
            .unwrap();
        let worker = Checkpointer::start(conn, Duration::from_millis(100), None).unwrap();
        let until = Instant::now() + Duration::from_secs(5);
        while !worker.failed.load(Ordering::Acquire) && Instant::now() < until {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(worker.failed.load(Ordering::Acquire));
        drop(worker);

        let worker = Checkpointer::start(
            Connection::open_in_memory().unwrap(),
            Duration::from_secs(60),
            None,
        )
        .unwrap();
        for _ in 0..10_000 {
            worker.notify();
        }
        let start = Instant::now();
        drop(worker);
        assert!(start.elapsed() < Duration::from_secs(5));
    }
}

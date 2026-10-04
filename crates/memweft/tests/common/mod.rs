use std::{io, path::Path, time::Duration};

/// Remove a test database only after all of its Memory/UserMemory handles drop.
/// r2d2's initializer/reaper can briefly retain a pool Arc after the last owner
/// drops. Windows cannot unlink the database until those connections close.
/// This is bounded test cleanup, not a synchronous SDK shutdown guarantee.
pub fn remove_database(path: impl AsRef<Path>) -> io::Result<()> {
    remove_with_retry(
        || std::fs::remove_file(path.as_ref()),
        cfg!(windows),
        100,
        || std::thread::sleep(Duration::from_millis(20)),
    )
}

pub(crate) fn remove_with_retry(
    mut remove: impl FnMut() -> io::Result<()>,
    windows: bool,
    retries: usize,
    mut pause: impl FnMut(),
) -> io::Result<()> {
    for attempt in 0..=retries {
        match remove() {
            // ERROR_SHARING_VIOLATION only; do not hide permission, missing-file,
            // or other I/O errors. A persistently held handle still fails.
            Err(error) if windows && error.raw_os_error() == Some(32) && attempt < retries => {
                pause();
            }
            result => return result,
        }
    }
    unreachable!()
}

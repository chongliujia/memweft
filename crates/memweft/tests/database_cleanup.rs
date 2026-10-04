mod common;

use std::{cell::Cell, io};

#[test]
fn cleanup_removes_file_and_reports_missing_file() {
    let path = std::env::temp_dir().join(format!("cleanup-{}.db", uuid::Uuid::new_v4()));
    std::fs::write(&path, b"closed database fixture").unwrap();
    common::remove_database(&path).unwrap();
    assert!(!path.exists());
    assert_eq!(
        common::remove_database(&path).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
}

#[test]
fn sharing_violation_can_recover_without_hiding_other_errors() {
    let attempts = Cell::new(0);
    let pauses = Cell::new(0);
    common::remove_with_retry(
        || {
            attempts.set(attempts.get() + 1);
            if attempts.get() < 3 {
                Err(io::Error::from_raw_os_error(32))
            } else {
                Ok(())
            }
        },
        true,
        3,
        || pauses.set(pauses.get() + 1),
    )
    .unwrap();
    assert_eq!((attempts.get(), pauses.get()), (3, 2));

    // Windows permission denied is not a sharing violation. Error 32 on Unix
    // is unrelated too. Neither may be converted into a successful cleanup.
    for (windows, code) in [(true, 5), (true, 2), (false, 32)] {
        attempts.set(0);
        let error = common::remove_with_retry(
            || {
                attempts.set(attempts.get() + 1);
                Err(io::Error::from_raw_os_error(code))
            },
            windows,
            3,
            || panic!("unexpected retry"),
        )
        .unwrap_err();
        assert_eq!(error.raw_os_error(), Some(code));
        assert_eq!(attempts.get(), 1);
    }
}

#[test]
fn persistent_sharing_violation_exhausts_the_bound_and_still_fails() {
    let attempts = Cell::new(0);
    let pauses = Cell::new(0);
    let error = common::remove_with_retry(
        || {
            attempts.set(attempts.get() + 1);
            Err(io::Error::from_raw_os_error(32))
        },
        true,
        3,
        || pauses.set(pauses.get() + 1),
    )
    .unwrap_err();
    assert_eq!(error.raw_os_error(), Some(32));
    assert_eq!((attempts.get(), pauses.get()), (4, 3));
}

#[cfg(windows)]
#[test]
fn windows_locked_file_fails_until_the_owner_releases_it() {
    use std::{fs::OpenOptions, os::windows::fs::OpenOptionsExt};
    let path = std::env::temp_dir().join(format!("cleanup-locked-{}.db", uuid::Uuid::new_v4()));
    std::fs::write(&path, b"locked database fixture").unwrap();
    let mut held = Some(
        OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&path)
            .unwrap(),
    );

    let error =
        common::remove_with_retry(|| std::fs::remove_file(&path), true, 2, || {}).unwrap_err();
    assert_eq!(error.raw_os_error(), Some(32));
    assert!(path.exists());

    // Deterministically release the real Windows handle after the first failed
    // deletion; no timing assumptions or unbounded wait are needed in this test.
    let attempts = Cell::new(0);
    common::remove_with_retry(
        || {
            attempts.set(attempts.get() + 1);
            let result = std::fs::remove_file(&path);
            drop(held.take());
            result
        },
        true,
        2,
        || {},
    )
    .unwrap();
    assert_eq!(attempts.get(), 2);
    assert!(!path.exists());
}

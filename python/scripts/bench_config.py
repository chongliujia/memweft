#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return repo_root() / "bench" / "memweft_bench.env"


def load_env_file(path: Path) -> dict[str, str]:
    env_map: dict[str, str] = {}
    if not path.exists():
        return env_map
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env_map[key] = value
    return env_map


def apply_env_defaults(env_map: dict[str, str]) -> None:
    for key, value in env_map.items():
        if key not in os.environ:
            os.environ[key] = value


def load_bench_env(path: Optional[str] = None) -> Optional[Path]:
    if path:
        config_path = Path(path)
    else:
        env_path = os.getenv("MEMWEFT_BENCH_CONFIG")
        if env_path:
            config_path = Path(env_path)
        else:
            config_path = default_config_path()
            if not config_path.exists():
                return None
    env_map = load_env_file(config_path)
    apply_env_defaults(env_map)
    return config_path


def find_config_arg(argv: list[str]) -> Optional[str]:
    for idx, arg in enumerate(argv):
        if arg == "--config":
            if idx + 1 < len(argv):
                return argv[idx + 1]
            return None
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_str(key: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw

import sys
from pathlib import Path

import pytest

from runtrace.process_runner import ProcessRunner
from runtrace.run_store import RunStore


@pytest.fixture
def store(tmp_path: Path):
    db = tmp_path / "runtrace-test.db"
    instance = RunStore(db)
    yield instance
    instance.close()


@pytest.fixture
def runner(store: RunStore) -> ProcessRunner:
    return ProcessRunner(store)


@pytest.fixture
def python_exe() -> str:
    return sys.executable

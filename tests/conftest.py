"""Session hygiene for state the harness sets on the interpreter itself."""

import tempfile
import threading

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Keep UID changes from sharing pytest's username-derived temp root."""
    if config.option.basetemp is None:
        temporary = tempfile.TemporaryDirectory(prefix="steward-pytest-")
        config.option.basetemp = temporary.name
        config.add_cleanup(temporary.cleanup)


@pytest.fixture(autouse=True)
def no_thread_may_die_unnoticed():
    """Fail a test whose background thread died, and contain `steward run`.

    Two jobs, because they are the same hook. Services used to expose
    `raise_if_failed()` so a test could ask whether one of their loops had
    latched an exception; the latch is gone, and what replaced it is
    `threading.excepthook`, so the question is asked here instead — once, for
    every thread in every test, including the ones no service owned.

    Containment matters because `steward run` points that hook at
    `os._exit`. The hook is global and the suite is one process, so a test
    exercising the entry point would otherwise arm it for every test after
    it, and the first thread to die anywhere would end the run with no
    summary.
    """
    original = threading.excepthook
    deaths: list[str] = []

    def record(args) -> None:
        deaths.append(f"{getattr(args.thread, 'name', '?')}: {args.exc_value!r}")
        original(args)

    threading.excepthook = record
    try:
        yield deaths
    finally:
        threading.excepthook = original
    assert not deaths, f"a background thread died: {deaths}"

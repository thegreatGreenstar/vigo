"""Run the suite on several threads, with nothing but the standard library.

    python3 -m tests.parallel                 # a thread per core, capped at 8
    python3 -m tests.parallel --workers 4
    python3 -m tests.parallel --pattern "test_app*"

The suite is safe to run this way because nothing in it is shared: every test
builds its own temporary directory, its settings file, and its own fakes, and
those bind port 0 so two of them never want the same port.  `unittest` is not
thread-safe, so each case gets its own result object and the output is printed
by this thread alone, in order.

The one thing that cannot be split is a class with class-level fixtures: its
cases are kept on one thread, in their declared order, so `setUpClass` runs
once around them.  `python -m unittest discover -s tests -t .` stays the
canonical run — CI uses it, and it would catch a fault in this file.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable, Iterator


def cases_of(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """Every case in a suite, with its nested suites flattened."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases_of(item)
        else:
            yield item


def _function(method) -> object:
    """The plain function behind a classmethod, so two lookups compare equal."""
    return getattr(method, "__func__", method)


def has_fixtures(case: unittest.TestCase) -> bool:
    """Whether this case's class runs anything around the whole class."""
    cls = type(case)
    plain = unittest.TestCase
    return (_function(cls.setUpClass) is not _function(plain.setUpClass)
            or _function(cls.tearDownClass) is not _function(plain.tearDownClass))


def work_items(cases: Iterable[unittest.TestCase]) -> list[unittest.TestSuite]:
    """The units to hand to a thread: one case, or a class's cases together."""
    kept: dict[str, list[unittest.TestCase]] = {}
    alone: list[unittest.TestSuite] = []
    for case in cases:
        if isinstance(case, unittest.TestCase) and has_fixtures(case):
            kept.setdefault(type(case).__qualname__, []).append(case)
        else:
            alone.append(unittest.TestSuite([case]))
    return alone + [unittest.TestSuite(group) for group in kept.values()]


def run_one(item: unittest.TestSuite) -> tuple[unittest.TestResult, float, str]:
    """Run one unit and return what happened, without printing anything."""
    # The suite clears itself as it runs, so the name has to be taken first.
    name = " ".join(str(case) for case in item) or "empty"
    result = unittest.TestResult()
    started = time.monotonic()
    item.run(result)
    return result, time.monotonic() - started, name


def report(results: list[tuple[unittest.TestResult, float, str]], elapsed: float,
           out, slowest: int = 0) -> int:
    """Every failure in one place, then the line the canonical runner prints."""
    ran = sum(result.testsRun for result, _seconds, _name in results)
    failures = [pair for result, _seconds, _name in results for pair in result.failures]
    errors = [pair for result, _seconds, _name in results for pair in result.errors]
    skipped = [pair for result, _seconds, _name in results for pair in result.skipped]
    unexpected = [case for result, _seconds, _name in results
                  for case in result.unexpectedSuccesses]

    if slowest:
        ranked = sorted(results, key=lambda entry: entry[1], reverse=True)[:slowest]
        out.write("\nslowest:")
        for _result, seconds, name in ranked:
            out.write(f"\n  {seconds:6.2f}s  {name}")
        out.write("\n")

    for title, found in (("FAIL", failures), ("ERROR", errors), ("UNEXPECTED SUCCESS", unexpected)):
        for entry in sorted(found, key=lambda pair: str(pair[0])):
            case, why = entry if isinstance(entry, tuple) else (entry, "")
            out.write(f"\n{title}: {case}\n{'-' * 70}\n{why}\n")
    if skipped:
        out.write("\nskipped: " + ", ".join(str(case) for case, _why in skipped) + "\n")

    out.write(f"\nRan {ran} test{'s' if ran != 1 else ''} in {elapsed:.3f}s\n")
    bad = len(failures) + len(errors) + len(unexpected)
    if not bad:
        out.write("OK\n")
        return 0
    parts = []
    if failures:
        parts.append(f"failures={len(failures)}")
    if errors:
        parts.append(f"errors={len(errors)}")
    if unexpected:
        parts.append(f"unexpected successes={len(unexpected)}")
    out.write(f"FAILED ({', '.join(parts)})\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.parallel", description=__doc__)
    parser.add_argument("--workers", type=int, default=0,
                        help="how many threads; 0 means one per core, capped at 8")
    parser.add_argument("--start", default="tests", help="where the test files are")
    parser.add_argument("--top", default=".", help="the directory the tests import from")
    parser.add_argument("--pattern", default="test*.py", help="which files to run")
    parser.add_argument("--slowest", type=int, default=0,
                        help="name the N slowest cases, to see where the time goes")
    args = parser.parse_args(argv)

    workers = args.workers or min(8, os.cpu_count() or 2)
    suite = unittest.defaultTestLoader.discover(
        args.start, pattern=args.pattern, top_level_dir=args.top,
    )
    items = work_items(cases_of(suite))
    if not items:
        sys.stderr.write(f"no tests found under {args.start}\n")
        return 2

    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for entry in pool.map(run_one, items):
            results.append(entry)
            outcome = "." if entry[0].wasSuccessful() else "F"
            sys.stdout.write(outcome)
            sys.stdout.flush()
    elapsed = time.monotonic() - started
    sys.stdout.write("\n")
    return report(results, elapsed, sys.stdout, args.slowest)


if __name__ == "__main__":
    sys.exit(main())

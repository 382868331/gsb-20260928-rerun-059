from __future__ import annotations

import unittest

from proto_robust.generator import SimpleMessageGenerator
from proto_robust.models import FailureSignature, Outcome, TargetResult
from proto_robust.reducer import reduce_case
from proto_robust.runner import IsolatedWorker, run_isolated
from targets.simple_message import parse_message


def slow_target(_: bytes) -> None:
    if _ == b"fast":
        return
    import time

    time.sleep(2.0)


def native_exit_target(_: bytes) -> None:
    if _ == b"survive":
        return
    import os

    os._exit(7)


def quiet_target(_: bytes) -> None:
    return None


class SimpleMessageSystemTests(unittest.TestCase):
    def test_generator_is_seed_deterministic(self) -> None:
        first = [case.data for case, _ in zip(SimpleMessageGenerator(7), range(40), strict=False)]
        second = [case.data for case, _ in zip(SimpleMessageGenerator(7), range(40), strict=False)]
        self.assertEqual(first, second)

    def test_rejection_is_distinct_from_crash(self) -> None:
        rejected = run_isolated(parse_message, b"XX", 1.0)
        crashing = run_isolated(parse_message, b"SM\x01\x00\x00\x01\xff", 1.0)
        self.assertIs(rejected.outcome, Outcome.REJECTED)
        self.assertIs(crashing.outcome, Outcome.CRASH)
        self.assertEqual(crashing.exception_type, "IndexError")

    def test_reducer_preserves_crash_type_and_key_byte(self) -> None:
        original = b"SM\x01\x00\x00\x05\xffAAAA"
        signature = FailureSignature(Outcome.CRASH, "IndexError", "list index out of range")

        def run(data: bytes) -> TargetResult:
            try:
                parse_message(data)
            except Exception as exc:  # ParseReject intentionally changes outcome.
                outcome = Outcome.CRASH if type(exc).__name__ == "IndexError" else Outcome.REJECTED
                return TargetResult(outcome, 0.0, type(exc).__name__, str(exc))
            return TargetResult(Outcome.OK, 0.0)

        minimized, _, critical_offsets = reduce_case(original, signature, run)
        self.assertEqual(minimized, b"SM\x01\x00\x00\x01\xff")
        self.assertIn(6, critical_offsets)

    def test_timeout_signature_differs_from_crash(self) -> None:
        self.assertNotEqual(
            FailureSignature(Outcome.TIMEOUT).key(),
            FailureSignature(Outcome.CRASH, "IndexError").key(),
        )

    def test_worker_recovers_after_timeout_and_process_death(self) -> None:
        worker = IsolatedWorker("tests.test_system.slow_target")
        try:
            timeout = worker.run(b"x", 0.05)
            self.assertIs(timeout.outcome, Outcome.TIMEOUT)
            recovered = worker.run(b"fast", 1.0)
            self.assertIs(recovered.outcome, Outcome.OK)
            worker.close()

            dead = IsolatedWorker("tests.test_system.native_exit_target")
            failed = dead.run(b"x", 1.0)
            self.assertIs(failed.outcome, Outcome.RUNNER_ERROR)
            self.assertIn("7", failed.message)
            restarted = dead.run(b"survive", 1.0)
            self.assertIs(restarted.outcome, Outcome.OK)
        finally:
            worker.close()
            if 'dead' in locals():
                dead.close()


if __name__ == "__main__":
    unittest.main()

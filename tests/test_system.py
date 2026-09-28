import tempfile
import unittest
from pathlib import Path

from robustlab.budget import Budget
from robustlab.generator import SeededGenerator, build_frame
from robustlab.minimizer import SameFailureMinimizer
from robustlab.outcome import ExecutionResult, FailureSignature, Kind
from robustlab.parser import ParseError, parse_message
from robustlab.runner import SubprocessRunner
from robustlab.store import ResultStore

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    """按字节规则分类的内存执行器，用于只测缩小/编排逻辑。"""

    def __init__(self, timeout_s: float = 1.0):
        self.timeout_s = timeout_s
        self.calls = 0

    def run(self, sample: bytes) -> ExecutionResult:
        self.calls += 1
        try:
            parse_message(sample)
        except ParseError as exc:
            return ExecutionResult(kind=Kind.REJECTED, reason=str(exc))
        except IndexError:
            import traceback

            return ExecutionResult(
                kind=Kind.CRASH, exit_code=1,
                stderr=traceback.format_exc() + "  in _handle_group",
            )
        return ExecutionResult(kind=Kind.ACCEPTED)


class ParserTests(unittest.TestCase):
    def test_valid_frame_accepted(self):
        result = parse_message(build_frame(bytes([0x01, 1, 0x07])))
        self.assertEqual(result["fields"]["id"], b"\x07")

    def test_invalid_frames_are_rejected_not_crash(self):
        for bad in (b"", b"\x00" * 7, b"\xa5\x5a" + b"\x09" + b"\x00" * 4):
            with self.assertRaises(ParseError):
                parse_message(bad)

    def test_group_code_four_is_known_index_crash(self):
        crash_frame = build_frame(bytes([0xDD, 1, 0x04]))
        with self.assertRaises(IndexError):
            parse_message(crash_frame)


class GeneratorTests(unittest.TestCase):
    def test_fixed_seed_is_reproducible(self):
        first = [SeededGenerator(seed=42).generate(i) for i in range(50)]
        second = [SeededGenerator(seed=42).generate(i) for i in range(50)]
        self.assertEqual(first, second)
        other = [SeededGenerator(seed=43).generate(i) for i in range(50)]
        self.assertNotEqual(first, other)

    def test_deterministic_boundary_cases_exist(self):
        gen = SeededGenerator(seed=20260929)
        samples = [gen.generate(i) for i in range(12)]
        crash = build_frame(bytes([0xDD, 1, 0x04]))
        timeout = build_frame(bytes([0xEE, 1, 0x02]))
        self.assertIn(crash, samples)
        self.assertIn(timeout, samples)


class OutcomeSeparationTests(unittest.TestCase):
    def test_timeout_differs_from_crash_and_reject(self):
        crash = ExecutionResult(kind=Kind.CRASH).signature()
        timeout = ExecutionResult(kind=Kind.TIMEOUT).signature()
        rejected = ExecutionResult(kind=Kind.REJECTED).signature()
        self.assertIsNone(rejected)
        self.assertNotEqual(crash, timeout)
        self.assertTrue(Kind.CRASH.is_failure)
        self.assertTrue(Kind.TIMEOUT.is_failure)
        self.assertFalse(Kind.REJECTED.is_failure)


class MinimizerTests(unittest.TestCase):
    def test_minimizer_preserves_crash_kind_never_substitutes_reject(self):
        runner = FakeRunner()
        # 合法帧 + 两个垃圾 TLV，整体仍会走到 group 缺陷
        padded = build_frame(
            bytes([0x01, 2, 0xAA, 0xBB, 0x33, 4, 1, 2, 3, 4, 0xDD, 1, 0x04])
        )
        sig = FailureSignature(Kind.CRASH, "IndexError@_handle_group")
        result = SameFailureMinimizer(runner).minimize(padded, sig)
        self.assertEqual(result.signature.kind, Kind.CRASH)
        with self.assertRaises(IndexError):
            parse_message(result.sample)
        self.assertLess(len(result.sample), len(padded))


class StoreTests(unittest.TestCase):
    def test_duplicate_failures_deduped_by_kind_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp), "t")
            crash_sig = FailureSignature(Kind.CRASH, "IndexError@_handle_group")
            timeout_sig = FailureSignature(Kind.TIMEOUT, "deadline-exceeded")
            self.assertFalse(store.is_duplicate(crash_sig))
            store.save_failure(
                b"x", ExecutionResult(kind=Kind.CRASH, exit_code=1), crash_sig, 0
            )
            self.assertTrue(store.is_duplicate(crash_sig))
            self.assertFalse(store.is_duplicate(timeout_sig))


class BudgetTests(unittest.TestCase):
    def test_budget_limits_iterations_and_first_failure(self):
        budget = Budget(max_iterations=3, stop_after_first_failure=True)
        budget.start()
        budget.tick(new_failure=True)
        self.assertTrue(budget.exhausted)
        budget2 = Budget(max_iterations=2)
        budget2.start()
        budget2.tick()
        self.assertFalse(budget2.exhausted)
        budget2.tick()
        self.assertTrue(budget2.exhausted)


@unittest.skipUnless(
    (REPO_ROOT / "robustlab").exists(), "需要从仓库根目录运行真实子进程测试"
)
class SubprocessRunnerTests(unittest.TestCase):
    def test_real_runner_classifies_three_paths(self):
        runner = SubprocessRunner(timeout_s=0.4, cwd=REPO_ROOT)
        accepted = runner.run(build_frame(bytes([0x01, 1, 0x01])))
        rejected = runner.run(b"\x00" * 8)
        crashed = runner.run(build_frame(bytes([0xDD, 1, 0x05])))
        timed_out = runner.run(build_frame(bytes([0xEE, 1, 0x02])))
        self.assertEqual(accepted.kind, Kind.ACCEPTED)
        self.assertEqual(rejected.kind, Kind.REJECTED)
        self.assertEqual(crashed.kind, Kind.CRASH)
        self.assertEqual(timed_out.kind, Kind.TIMEOUT)
        self.assertEqual(timed_out.exit_code, -9)


if __name__ == "__main__":
    unittest.main()

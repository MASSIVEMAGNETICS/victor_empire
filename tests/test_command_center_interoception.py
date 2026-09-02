import unittest

from command_center import command_center_status
from command_center_interoception import project_devville_body_state
from victor_body_state import AuthorityState
from victor_runtime.organs import DEVVILLE_CAPABILITY, DEVVILLE_ORGAN


class ProbeOnlyRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.probe_calls = 0
        self.dispatch_calls = 0

    def probe(self):
        self.probe_calls += 1
        if self.fail:
            raise RuntimeError("probe unavailable")
        return {
            "organ": DEVVILLE_ORGAN,
            "capabilities": [DEVVILLE_CAPABILITY],
            "status": "ready",
        }

    def dispatch(self, *args, **kwargs):
        self.dispatch_calls += 1
        raise AssertionError("read-only status must never dispatch work")


class KernelStub:
    def __init__(self, runner: ProbeOnlyRunner) -> None:
        self.devville = runner
        self.status_calls = 0
        self.run_goal_calls = 0

    def status(self):
        self.status_calls += 1
        return {"event_chain_ok": True, "allowed_capabilities": [DEVVILLE_CAPABILITY]}

    def run_goal(self, *args, **kwargs):
        self.run_goal_calls += 1
        raise AssertionError("status projection must never execute a goal")


class CommandCenterInteroceptionTests(unittest.TestCase):
    def test_reachable_probe_is_visible_but_unresolved_and_unstable(self) -> None:
        runner = ProbeOnlyRunner()
        result = project_devville_body_state(runner)

        self.assertEqual(result["mode"], "read_only")
        self.assertEqual(result["authority"], "unknown")
        self.assertEqual(result["authority_resolution"], "unresolved")
        self.assertEqual(result["observation_detail"], "probe_reachable_only")
        self.assertEqual(result["body"]["unknown_authority_organs"], (DEVVILLE_ORGAN,))
        self.assertEqual(result["body"]["unverified_organs"], (DEVVILLE_ORGAN,))
        self.assertFalse(result["body"]["stable"])
        self.assertEqual(runner.probe_calls, 1)
        self.assertEqual(runner.dispatch_calls, 0)

    def test_failed_probe_degrades_without_dispatch(self) -> None:
        runner = ProbeOnlyRunner(fail=True)
        result = project_devville_body_state(runner, authority=AuthorityState.BLOCKED)

        self.assertEqual(result["authority"], "blocked")
        self.assertEqual(result["authority_resolution"], "supplied_by_control_plane")
        self.assertEqual(result["body"]["blocked_organs"], (DEVVILLE_ORGAN,))
        self.assertEqual(result["body"]["degraded_organs"], (DEVVILLE_ORGAN,))
        self.assertFalse(result["body"]["stable"])
        self.assertEqual(runner.dispatch_calls, 0)

    def test_command_center_status_adds_observation_without_execution(self) -> None:
        runner = ProbeOnlyRunner()
        kernel = KernelStub(runner)

        result = command_center_status(kernel)  # type: ignore[arg-type]

        self.assertTrue(result["event_chain_ok"])
        self.assertIn("interoception", result)
        self.assertEqual(result["interoception"]["organ"], DEVVILLE_ORGAN)
        self.assertFalse(result["interoception"]["body"]["stable"])
        self.assertEqual(kernel.status_calls, 1)
        self.assertEqual(kernel.run_goal_calls, 0)
        self.assertEqual(runner.dispatch_calls, 0)

    def test_explicit_active_authority_does_not_upgrade_unverified_probe_to_stable(self) -> None:
        runner = ProbeOnlyRunner()
        result = project_devville_body_state(runner, authority=AuthorityState.ACTIVE)

        self.assertEqual(result["body"]["unknown_authority_organs"], ())
        self.assertEqual(result["body"]["unverified_organs"], (DEVVILLE_ORGAN,))
        self.assertFalse(result["body"]["stable"])
        self.assertEqual(runner.dispatch_calls, 0)


if __name__ == "__main__":
    unittest.main()

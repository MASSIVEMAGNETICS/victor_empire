from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from inbox_capture import InboxCapture
from runway_ledger import RunwayLedger
from single_thread_enforcer import SingleThreadEnforcer
from victor_nzt_mode_router import route_mode
from work_order import WorkOrder


def start_session(
    *,
    goal: str,
    definition_of_done: str,
    next_actions: Optional[List[str]],
    blockers: Optional[List[str]],
    timebox_minutes: Optional[int],
    self_reported_state: Optional[str],
    tab_switch_pressure: Optional[int],
    last_output_minutes_ago: Optional[int],
    inbox_items: Optional[List[str]],
    inbox_tag: str,
    inbox_schedule: str,
    minutes_worked: Optional[int],
    friction_events: Optional[str],
) -> Path:
    work_order = WorkOrder(
        goal=goal,
        definition_of_done=definition_of_done,
        next_actions=next_actions or [],
        blockers=blockers or [],
        timebox_minutes=timebox_minutes,
    )

    mode = route_mode(
        self_reported_state=self_reported_state,
        tab_switch_pressure=tab_switch_pressure,
        last_output_minutes_ago=last_output_minutes_ago,
    )

    enforcer = SingleThreadEnforcer()
    enforcer.ensure_single_active(work_order)

    wo_path = Path(f"work_order_{work_order.id}.json")
    work_order.save(wo_path)

    inbox = InboxCapture()
    for item in inbox_items or []:
        inbox.capture(item, tag=inbox_tag, schedule=inbox_schedule, work_order_id=work_order.id)

    ledger = RunwayLedger()
    ledger.append(
        mode=mode,
        work_order_id=work_order.id,
        artifact=str(wo_path),
        minutes_worked=minutes_worked,
        friction_events=friction_events,
    )

    return wo_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Victor NZT Session CLI")
    parser.add_argument("--start", action="store_true", help="Start a new session")
    parser.add_argument("--goal", default="Unspecified goal", help="Goal for the work order")
    parser.add_argument(
        "--definition-of-done",
        default="Record an output and update the ledger",
        help="Definition of done for the work order",
    )
    parser.add_argument("--next", dest="next_actions", action="append", help="Next actions (repeatable)")
    parser.add_argument("--blocker", dest="blockers", action="append", help="Blockers (repeatable)")
    parser.add_argument("--timebox", type=int, dest="timebox_minutes", help="Timebox in minutes")
    parser.add_argument("--state", dest="self_reported_state", help="Self-reported state (e.g., focused, tired)")
    parser.add_argument("--tabs", dest="tab_switch_pressure", type=int, help="Tab-switch pressure 1-10")
    parser.add_argument(
        "--last-output-minutes",
        dest="last_output_minutes_ago",
        type=int,
        help="Minutes since last shipped output",
    )
    parser.add_argument(
        "--inbox",
        dest="inbox_items",
        action="append",
        help="Inbox item to capture (repeatable)",
    )
    parser.add_argument("--inbox-tag", default="idea", help="Tag to use for inbox captures")
    parser.add_argument("--inbox-schedule", default="later", help="Schedule to use for inbox captures")
    parser.add_argument("--minutes", dest="minutes_worked", type=int, help="Minutes worked this session")
    parser.add_argument("--friction", dest="friction_events", help="Friction events description")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.start:
        parser.error("The --start flag is required; only --start is supported in this CLI.")

    wo_path = start_session(
        goal=args.goal,
        definition_of_done=args.definition_of_done,
        next_actions=args.next_actions,
        blockers=args.blockers,
        timebox_minutes=args.timebox_minutes,
        self_reported_state=args.self_reported_state,
        tab_switch_pressure=args.tab_switch_pressure,
        last_output_minutes_ago=args.last_output_minutes_ago,
        inbox_items=args.inbox_items,
        inbox_tag=args.inbox_tag,
        inbox_schedule=args.inbox_schedule,
        minutes_worked=args.minutes_worked,
        friction_events=args.friction_events,
    )

    print(f"Started work order at {wo_path}")
    print("Ledger updated; inbox captured." if args.inbox_items else "Ledger updated.")


if __name__ == "__main__":
    main()

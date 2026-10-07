"""응답 전환·복구 또는 shadow 실행·중지 시나리오 진입점."""

import argparse

from scenarios.distribution import validate_parameters
from scenarios.runner import Stage, handle_termination, run_stages


def main() -> int:
    """CLI에서 선택한 순차 전환 시나리오 실행 후 종료 코드 반환."""
    parser = argparse.ArgumentParser(description="Run sequential Docker migration scenarios")
    commands = parser.add_subparsers(dest="scenario", required=True)
    serving = commands.add_parser("serving", help="v1 to split to v2 to v1, with shadow stopped")
    serving.add_argument("--requests", type=int, default=1000)
    serving.add_argument("--tolerance", type=float, default=0.08)
    shadow = commands.add_parser("shadow", help="enable and stop shadow for both serving roles")
    shadow.add_argument("--requests", type=int, default=100)
    args = parser.parse_args()
    tolerance = args.tolerance if args.scenario == "serving" else 0
    try:
        validate_parameters(args.requests, 0.5, tolerance)
    except ValueError as error:
        parser.error(str(error))
    if args.scenario == "serving":
        stages = tuple(
            Stage(name, args.requests, ratio, tolerance=tolerance)
            for name, ratio in (("01-v1", 0), ("02-split", 0.5), ("03-v2", 1), ("04-rollback", 0))
        )
    else:
        stages = tuple(
            Stage(name, args.requests, ratio, enabled, 0)
            for name, ratio, enabled in (
                ("01-v1-shadow-on", 0, True),
                ("02-v1-shadow-off", 0, False),
                ("03-v2-shadow-on", 1, True),
                ("04-v2-shadow-off", 1, False),
            )
        )
    handle_termination()
    return run_stages(args.scenario, stages)


if __name__ == "__main__":
    raise SystemExit(main())

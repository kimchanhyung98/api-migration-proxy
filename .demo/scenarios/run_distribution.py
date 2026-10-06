import argparse

from scenarios.distribution import validate_parameters
from scenarios.runner import Stage, handle_termination, run_stages


def run(*, requests: int, v2_ratio: float, tolerance: float) -> int:
    return run_stages(
        "distribution",
        (Stage("distribution", requests, v2_ratio, tolerance=tolerance),),
        distribution=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an isolated Docker serving distribution check"
    )
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--v2-ratio", type=float, default=0.5)
    parser.add_argument("--tolerance", type=float, default=0.08)
    args = parser.parse_args()
    try:
        validate_parameters(args.requests, args.v2_ratio, args.tolerance)
    except ValueError as error:
        parser.error(str(error))
    handle_termination()
    return run(requests=args.requests, v2_ratio=args.v2_ratio, tolerance=args.tolerance)


if __name__ == "__main__":
    raise SystemExit(main())

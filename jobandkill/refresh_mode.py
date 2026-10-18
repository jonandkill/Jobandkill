"""Resolve refresh intent without credentials; missing/invalid input is read-only."""
from __future__ import annotations

import json
import os
from pathlib import Path


def resolve_mode(event_name: str, inputs: object) -> dict[str, bool]:
    inputs = inputs if isinstance(inputs, dict) else {}
    diagnose = inputs.get("diagnose_only")
    full = inputs.get("full_sync")
    explicit_refresh = diagnose is False or (type(diagnose) is str and diagnose == "false")
    write = event_name == "schedule" or (event_name == "workflow_dispatch" and explicit_refresh)
    return {
        "write_enabled": write,
        "full_sync": write and event_name == "workflow_dispatch"
        and (full is True or (type(full) is str and full == "true")),
    }


def main() -> int:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if not isinstance(event, dict):
            raise ValueError()
        mode = resolve_mode(os.getenv("GITHUB_EVENT_NAME", ""), event.get("inputs"))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write("".join(f"{key}={str(value).lower()}\n" for key, value in mode.items()))
    except (OSError, ValueError, KeyError):
        print("실행 모드를 확인할 수 없어 중단합니다. 운영 쓰기는 허용되지 않습니다.")
        return 2
    print("실행 모드: " + ("운영 갱신" if mode["write_enabled"] else "읽기 전용 진단"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

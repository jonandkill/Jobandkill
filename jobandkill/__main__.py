from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .auth import cleanup_personal_data, environment
from .config import configuration_report, draft_retention_days, require_production_settings
from .db import connect, initialize, list_sources, public_stats
from .ingest import (
    ConfigurationError,
    SOURCE_FACTORIES,
    get_attachment_rights_review,
    import_file,
    process_documents,
    set_attachment_rights,
    sync_source,
)
from .server import serve


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Job&Kill 공공기관 직무 데이터 수집·작성 솔루션")
    root.add_argument("--db", type=Path, help="SQLite 전용 경로(운영 DATABASE_URL보다 우선)")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="관리자 권한으로 데이터베이스 스키마 초기화·마이그레이션")
    commands.add_parser("status", help="수집 현황 확인")
    doctor_parser = commands.add_parser("doctor", help="비밀값을 노출하지 않고 운영 설정 점검")
    doctor_parser.add_argument("--production", action="store_true", help="운영 필수 설정 기준으로 점검")
    doctor_parser.add_argument("--require-api", action="store_true", help="공식 API 수집 설정도 필수로 점검")
    doctor_parser.add_argument("--collector-only", action="store_true", help="수집 작업에 필요한 설정만 점검")

    serve_parser = commands.add_parser("serve", help="웹/API 서버 실행")
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    sync_parser = commands.add_parser("sync", help="공식 소스 동기화")
    sync_group = sync_parser.add_mutually_exclusive_group(required=True)
    sync_group.add_argument("--source", choices=sorted(SOURCE_FACTORIES))
    sync_group.add_argument("--all", action="store_true", help="활성화된 자동 소스 모두 동기화")
    sync_parser.add_argument("--full", action="store_true", help="최초 전체 수집용으로 최대 1,000페이지 조회")

    import_parser = commands.add_parser("import", help="공식 API JSON/XML 응답 파일 가져오기")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--source", default="data-go-kr-alio")

    document_parser = commands.add_parser("process-documents", help="권리 승인된 첨부문서 추출")
    document_parser.add_argument("--limit", type=int, default=20)
    document_parser.add_argument(
        "--fail-on-error", action="store_true",
        help="문서 처리·객체 정리 실패 또는 격리 발생 시 종료 코드 4 반환",
    )

    cleanup_parser = commands.add_parser(
        "cleanup-personal-data",
        help="보존 기간이 지난 개인 데이터 정리(기본값은 삭제하지 않는 점검)",
    )
    cleanup_parser.add_argument(
        "--execute", action="store_true", help="점검 결과를 실제로 삭제",
    )

    rights_info_parser = commands.add_parser(
        "rights-info", help="권리 판정 전 현재 첨부 식별정보와 검토 토큰 확인"
    )
    rights_info_parser.add_argument("attachment_id", type=int)

    rights_parser = commands.add_parser("rights", help="첨부문서 이용권리 판정 기록")
    rights_parser.add_argument("attachment_id", type=int)
    rights_parser.add_argument(
        "status", choices=("open_document", "authorized", "metadata_only", "restricted", "review_required")
    )
    rights_parser.add_argument("--reason", required=True)
    rights_parser.add_argument("--by", required=True, dest="decided_by")
    rights_parser.add_argument(
        "--expected-identity",
        help="허용 판정 시 rights-info에서 확인한 현재 첨부 식별 해시",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            # This is the sole command-line schema administration path.  Normal
            # web, collector, and cleanup startup honors JOBNKILL_AUTO_MIGRATE.
            target = initialize(args.db, force_migrate=True)
            backend = "postgresql" if isinstance(target, str) and target.startswith(("postgresql://", "postgres://")) else "sqlite"
            _print({"database_backend": backend, "status": "initialized"})
        elif args.command == "serve":
            if environment() == "production":
                require_production_settings()
            serve(args.host, args.port, args.db)
        elif args.command == "doctor":
            report = configuration_report(args.production, args.require_api, args.collector_only)
            _print(report)
            return 0 if report["ready"] else 2
        elif args.command == "status":
            target = initialize(args.db)
            with connect(target) as connection:
                _print({"stats": public_stats(connection), "sources": list_sources(connection)})
        elif args.command == "sync":
            if args.full and "JOBNKILL_MAX_PAGES" not in os.environ:
                os.environ["JOBNKILL_MAX_PAGES"] = "1000"
            if args.all:
                target = initialize(args.db)
                with connect(target) as connection:
                    slugs = [
                        row["slug"] for row in connection.execute("SELECT slug FROM sources WHERE enabled ORDER BY id")
                        if row["slug"] in SOURCE_FACTORIES
                    ]
                results = [sync_source(slug, target) for slug in slugs]
                _print(results)
                if any(item.get("status") != "success" for item in results):
                    return 3
            else:
                result = sync_source(args.source, args.db)
                _print(result)
                if result.get("status") != "success":
                    return 3
        elif args.command == "import":
            _print(import_file(args.source, args.path, args.db))
        elif args.command == "process-documents":
            result = process_documents(args.db, args.limit)
            _print(result)
            if args.fail_on_error and (
                result.get("failed", 0) or result.get("gc_failed", 0)
                or result.get("gc_pending", 0) or result.get("quarantined", 0)
            ):
                return 4
        elif args.command == "cleanup-personal-data":
            target = initialize(args.db)
            with connect(target) as connection:
                _print(cleanup_personal_data(
                    connection, draft_retention_days(), execute=args.execute,
                ))
        elif args.command == "rights-info":
            _print(get_attachment_rights_review(args.attachment_id, args.db))
        elif args.command == "rights":
            set_attachment_rights(
                args.attachment_id, args.status, args.reason, args.decided_by, args.db,
                expected_identity=args.expected_identity,
            )
            _print({"attachment_id": args.attachment_id, "status": args.status, "updated": True})
        return 0
    except (ConfigurationError, ValueError, RuntimeError, OSError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

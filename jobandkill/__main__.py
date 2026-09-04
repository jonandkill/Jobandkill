from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import connect, initialize, list_sources, public_stats
from .ingest import (
    ConfigurationError,
    SOURCE_FACTORIES,
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
    root.add_argument("--db", type=Path, help="SQLite 데이터베이스 경로")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="데이터베이스 초기화")
    commands.add_parser("status", help="수집 현황 확인")

    serve_parser = commands.add_parser("serve", help="웹/API 서버 실행")
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    sync_parser = commands.add_parser("sync", help="공식 소스 동기화")
    sync_group = sync_parser.add_mutually_exclusive_group(required=True)
    sync_group.add_argument("--source", choices=sorted(SOURCE_FACTORIES))
    sync_group.add_argument("--all", action="store_true", help="활성화된 자동 소스 모두 동기화")

    import_parser = commands.add_parser("import", help="공식 API JSON/XML 응답 파일 가져오기")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--source", default="data-go-kr-alio")

    document_parser = commands.add_parser("process-documents", help="권리 승인된 첨부문서 추출")
    document_parser.add_argument("--limit", type=int, default=20)

    rights_parser = commands.add_parser("rights", help="첨부문서 이용권리 판정 기록")
    rights_parser.add_argument("attachment_id", type=int)
    rights_parser.add_argument(
        "status", choices=("open_document", "authorized", "metadata_only", "restricted", "review_required")
    )
    rights_parser.add_argument("--reason", required=True)
    rights_parser.add_argument("--by", required=True, dest="decided_by")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            _print({"database": str(initialize(args.db)), "status": "initialized"})
        elif args.command == "serve":
            serve(args.host, args.port, args.db)
        elif args.command == "status":
            initialize(args.db)
            with connect(args.db) as connection:
                _print({"stats": public_stats(connection), "sources": list_sources(connection)})
        elif args.command == "sync":
            if args.all:
                initialize(args.db)
                with connect(args.db) as connection:
                    slugs = [
                        row["slug"] for row in connection.execute("SELECT slug FROM sources WHERE enabled=1 ORDER BY id")
                        if row["slug"] in SOURCE_FACTORIES
                    ]
                _print([sync_source(slug, args.db) for slug in slugs])
            else:
                _print(sync_source(args.source, args.db))
        elif args.command == "import":
            _print(import_file(args.source, args.path, args.db))
        elif args.command == "process-documents":
            _print(process_documents(args.db, args.limit))
        elif args.command == "rights":
            set_attachment_rights(args.attachment_id, args.status, args.reason, args.decided_by, args.db)
            _print({"attachment_id": args.attachment_id, "status": args.status, "updated": True})
        return 0
    except (ConfigurationError, ValueError, RuntimeError, OSError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

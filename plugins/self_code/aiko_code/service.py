"""Editor-like reads, searches and syntax navigation over one explicit scope."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import time
from typing import Any

from .policy import Source, SourceError, SourceScope, _linked
from .syntax import SUPPORTED, parse


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SourceError(f"{name} must be an integer between {low} and {high}.")
    return value


def _query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\n" in value:
        raise SourceError("Provide a nonempty, single-line query of at most 200 characters.")
    return value


def _revision(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CodeTools:
    def __init__(self, config: dict[str, Any]):
        self.scope = SourceScope(config)

    def workspace_info(self) -> dict:
        """Read only minimal local Git identity; never run Git, filters or hooks."""
        head = None
        try:
            metadata = self.scope.root / ".git"

            def read_meta(parts, limit):
                path = metadata
                if not metadata.is_dir() or _linked(metadata):
                    raise SourceError("Git identity unavailable")
                for part in parts:
                    path /= part
                    if _linked(path):
                        raise SourceError("Linked Git metadata is not inspected")
                if not path.resolve().is_relative_to(metadata.resolve()):
                    raise SourceError("Git metadata is outside this checkout")
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(path, flags), "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                        raise SourceError("Git identity requires a regular, unlinked file")
                    raw = stream.read(limit + 1)
                if len(raw) > limit:
                    raise SourceError("Git metadata exceeds its identity budget")
                return raw.decode("ascii").strip()

            value = read_meta(["HEAD"], 4096)
            if value.startswith("ref: "):
                ref = value[5:]
                if not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", ref) or ".." in ref:
                    raise SourceError("Unrecognized Git reference")
                try:
                    value = read_meta(ref.split("/"), 128)
                except FileNotFoundError:
                    packed = read_meta(["packed-refs"], 1048576)
                    value = next(
                        (
                            line.split(" ", 1)[0]
                            for line in packed.splitlines()
                            if line.endswith(" " + ref)
                        ),
                        "",
                    )
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
                head = value
        except (OSError, UnicodeError, SourceError):
            pass
        return {
            "workspace": self.scope.root.name,
            "read_only": True,
            "include_paths": ["/".join(parts) or "." for parts in self.scope.includes],
            "checkout_head": head,
            "working_tree_state": "not_evaluated",
            "running_build_match": "not_verified",
            "syntax_languages": ["python", "javascript", "typescript", "tsx"],
            "reference_resolution": "syntactic_candidates_not_language_server_resolution",
            "syntax_checks": "parser_only_not_typechecking_or_test_execution",
            "limits": {
                "file_bytes": self.scope.max_file_bytes,
                "scan_files": self.scope.max_scan_files,
                "scan_bytes": self.scope.max_scan_bytes,
                "query_seconds": self.scope.max_query_seconds,
            },
            "note": "File hashes describe inspected contents, not a clean or running build.",
        }

    def _page(
        self, items, revision_data, reasons, *, offset, limit, revision=None, pinned=False, **extra
    ):
        offset = _integer(offset, "offset", 0, 100000)
        limit = _integer(limit, "limit", 1, 50)
        if offset and revision is None and not pinned:
            raise SourceError("Continuation requires the previous revision or source hash.")
        digest = _revision(revision_data)
        if revision is not None and revision != digest:
            raise SourceError("The search/source revision changed; restart the query at offset 0.")
        if offset > len(items):
            raise SourceError("The result offset is out of range.")
        selected = []
        for item in items[offset : offset + limit]:
            if len(json.dumps([*selected, item], ensure_ascii=True)) > 48000:
                break
            selected.append(item)
        end = offset + len(selected)
        return {
            "status": "partial" if reasons else "ok",
            "scope_complete": not reasons,
            "limited_by": sorted(reasons),
            "revision": digest,
            "offset": offset,
            "items": selected,
            "next_offset": end if end < len(items) else None,
            "matched_in_scanned_scope": len(items),
            **extra,
        }

    def find_files(self, pattern="**", offset=0, limit=50, revision=None):
        scan = self.scope.scan(pattern)
        identities = []
        for path in scan.files:
            if time.monotonic() >= scan.deadline:
                scan.reasons.add("time_limit")
                break
            try:
                info = self.scope.resolve(path).stat()
                identities.append(
                    {"path": path, "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
                )
            except (OSError, SourceError):
                scan.reasons.add("source_changed_or_unreadable")
        return self._page(
            identities,
            [pattern, identities],
            scan.reasons,
            offset=offset,
            limit=limit,
            revision=revision,
            pattern=pattern,
            note="Only permitted source paths are listed; partial scans do not prove absence.",
        )

    def read_code(self, path, start_line=1, end_line=None, start_column=1, expected_sha256=None):
        source = self.scope.read(path, expected_sha256)
        return self._read(source, start_line, end_line, start_column)

    def _read(self, source: Source, start_line, end_line, start_column):
        lines = source.lines
        start_line = _integer(start_line, "start_line", 1, max(1, len(lines)))
        start_column = _integer(start_column, "start_column", 1, self.scope.max_file_bytes + 1)
        if not lines:
            if start_column != 1 or end_line not in {None, 1}:
                raise SourceError("An empty source has no requested line range.")
            return {
                **source.identity(),
                "status": "ok",
                "lines": [],
                "total_lines": 0,
                "next": None,
                "is_fragment": False,
            }
        if start_column > len(lines[start_line - 1]) + 1:
            raise SourceError("start_column exceeds the requested source line.")
        end_line = len(lines) if end_line is None else end_line
        end_line = _integer(end_line, "end_line", start_line, max(start_line, len(lines)))
        requested_end, end_line = end_line, min(end_line, start_line + 199)
        remaining, selected, next_at = 8000, [], None
        for index in range(start_line - 1, min(end_line, len(lines))):
            column = start_column if index == start_line - 1 else 1
            text = lines[index][column - 1 :]
            fragment = text[:remaining]
            selected.append({"line": index + 1, "column": column, "text": fragment})
            remaining -= len(fragment) + 1
            if len(fragment) < len(text):
                next_at = {"start_line": index + 1, "start_column": column + len(fragment)}
                break
            if remaining <= 0:
                if index + 1 < requested_end:
                    next_at = {"start_line": index + 2, "start_column": 1}
                break
        if next_at is None and end_line < requested_end:
            next_at = {"start_line": end_line + 1, "start_column": 1}
        return {
            **source.identity(),
            "status": "ok",
            "lines": selected,
            "total_lines": len(lines),
            "requested_end_line": requested_end,
            "next": next_at,
            "is_fragment": start_line > 1
            or start_column > 1
            or requested_end < len(lines)
            or next_at is not None,
        }

    def file_symbols(self, path, offset=0, limit=50, expected_sha256=None):
        source = self.scope.read(path, expected_sha256)
        parsed = parse(source)
        result = self._page(
            parsed.symbols,
            source.sha256,
            set() if parsed.complete else {"syntax_error_or_parser_limit"},
            offset=offset,
            limit=limit,
            pinned=expected_sha256 is not None,
            source=source.identity(),
            language=parsed.language,
            parser=parsed.parser,
            problems=parsed.problems,
            resolution="syntactic_declarations",
        )
        if parsed.language == "unsupported":
            result["status"] = "unsupported"
        return result

    def read_symbol(self, path, name, expected_sha256=None):
        name = _query(name)
        source = self.scope.read(path, expected_sha256)
        parsed = parse(source)
        if parsed.language == "unsupported":
            return {
                "status": "unsupported",
                "source": source.identity(),
                "note": "Use read_code/search_code; this language has no registered syntax parser.",
            }
        found = [
            symbol
            for symbol in parsed.symbols
            if symbol["name_complete"] and name in {symbol["name"], symbol["qualified_name"]}
        ]
        if len(found) != 1:
            candidates = []
            for symbol in found[:20]:
                if len(json.dumps([*candidates, symbol], ensure_ascii=True)) > 40000:
                    break
                candidates.append(symbol)
            return {
                "status": "ambiguous" if found else "not_found",
                "source": source.identity(),
                "candidates": candidates,
                "candidates_omitted": len(candidates) < len(found),
                "scope_complete": parsed.complete,
                "note": "Use a qualified name or read_code with an explicit line range.",
            }
        symbol = found[0]
        return {
            **self._read(source, symbol["start_line"], symbol["end_line"], 1),
            "symbol": symbol,
            "parser": parsed.parser,
            "scope_complete": parsed.complete,
        }

    def syntax_problems(self, path, expected_sha256=None):
        source = self.scope.read(path, expected_sha256)
        parsed = parse(source)
        return {
            "status": "unsupported" if parsed.language == "unsupported" else "ok",
            "source": source.identity(),
            "language": parsed.language,
            "parser": parsed.parser,
            "problems": parsed.problems,
            "parse_complete": parsed.complete,
            "coverage": "syntax_only",
            "typechecked": False,
            "tests_executed": False,
            "note": "An empty syntax problem list does not prove type or runtime correctness.",
        }

    def _search(
        self,
        operation,
        query,
        pattern,
        *,
        offset,
        limit,
        revision,
        case_sensitive=False,
        exact=False,
    ):
        query = _query(query)
        scan = self.scope.scan(pattern)
        items, identities, bytes_read, files_read, unsupported = [], [], 0, 0, 0
        matcher = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
        for path in scan.files:
            if time.monotonic() >= scan.deadline:
                scan.reasons.add("time_limit")
                break
            if operation != "text" and PurePosixPath(path).suffix.lower() not in SUPPORTED:
                unsupported += 1
                continue
            try:
                info = self.scope.resolve(path).stat()
                if bytes_read + info.st_size > self.scope.max_scan_bytes:
                    scan.reasons.add("byte_limit")
                    break
                source = self.scope.read(path, max_bytes=self.scope.max_scan_bytes - bytes_read)
            except (OSError, SourceError):
                scan.reasons.add("unreadable_source")
                continue
            bytes_read += source.size
            files_read += 1
            identities.append([source.path, source.sha256])
            if operation == "text":
                for line, text in enumerate(source.lines, 1):
                    found = matcher.search(text)
                    if found:
                        begin = max(0, found.start() - 80)
                        items.append(
                            {
                                **source.identity(),
                                "line": line,
                                "column": found.start() + 1,
                                "excerpt": text[begin : begin + 240],
                                "excerpt_column": begin + 1,
                            }
                        )
                    if len(items) >= 1000:
                        break
            else:
                parsed = parse(source)
                if not parsed.complete:
                    scan.reasons.add("syntax_error_or_parser_limit")
                candidates = parsed.symbols if operation == "symbols" else parsed.identifiers
                declarations = {(s["line"], s["column"]) for s in parsed.symbols}
                for candidate in candidates:
                    name = candidate.get("qualified_name", candidate["name"])
                    matched = (
                        (candidate["name"] == query or name == query)
                        if exact
                        else bool(matcher.search(name))
                    )
                    if not matched:
                        continue
                    if operation == "symbols":
                        items.append(candidate)
                    else:
                        line, column = candidate["line"], candidate["column"]
                        text = source.lines[line - 1] if line <= len(source.lines) else ""
                        begin = max(0, column - 81)
                        items.append(
                            {
                                **source.identity(),
                                **candidate,
                                "excerpt": text[begin : begin + 240],
                                "excerpt_column": begin + 1,
                                "is_declaration": (line, column) in declarations,
                            }
                        )
                    if len(items) >= 1000:
                        break
            if len(items) >= 1000:
                scan.reasons.add("match_limit")
                break
        return self._page(
            items,
            [operation, query, pattern, exact, case_sensitive, identities],
            scan.reasons,
            offset=offset,
            limit=limit,
            revision=revision,
            query=query,
            pattern=pattern,
            scanned_files=files_read,
            scanned_bytes=bytes_read,
            unsupported_files_skipped=unsupported,
            scope_kind="permitted_text_sources"
            if operation == "text"
            else "supported_syntax_sources",
            syntax_languages=None
            if operation == "text"
            else ["python", "javascript", "typescript", "tsx"],
            resolution="identifier_candidates" if operation == "references" else operation,
            note="Reference candidates are not resolved callers or proof of symbol binding."
            if operation == "references"
            else "Results describe the permitted scanned source scope.",
        )

    def search_code(
        self, query, pattern="**", offset=0, limit=50, revision=None, case_sensitive=False
    ):
        return self._search(
            "text",
            query,
            pattern,
            offset=offset,
            limit=limit,
            revision=revision,
            case_sensitive=case_sensitive,
        )

    def find_symbol(self, query, pattern="**", offset=0, limit=50, revision=None, exact=False):
        return self._search(
            "symbols", query, pattern, offset=offset, limit=limit, revision=revision, exact=exact
        )

    def find_references(self, name, pattern="**", offset=0, limit=50, revision=None):
        if not isinstance(name, str) or not name.replace("$", "_").lstrip("#").isidentifier():
            raise SourceError(
                "Use an unqualified identifier name; this tool does not resolve bindings."
            )
        return self._search(
            "references",
            name,
            pattern,
            offset=offset,
            limit=limit,
            revision=revision,
            exact=True,
            case_sensitive=True,
        )

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class LocalTraceToolExecutor:
    def __init__(self, artifacts_dir: Path, trace_file: Path, repo_root: Path | None = None):
        self.artifacts_dir = artifacts_dir.resolve()
        self.trace_file = trace_file.resolve()
        self.repo_root = repo_root.resolve() if repo_root is not None else self._discover_repo_root()
        self.search_dir = self.repo_root / "tools" / "search"
        self.search_bin = self.search_dir / "ak_search"
        self.search_daemon: subprocess.Popen[str] | None = None

    def _discover_repo_root(self) -> Path:
        candidates = []
        env_root = os.getenv("HARNESS_REPO_ROOT")
        if env_root:
            candidates.append(Path(env_root).expanduser())
        candidates.extend(
            [
                Path.cwd(),
                Path(__file__).resolve().parents[2],
            ]
        )
        for candidate in candidates:
            root = candidate.resolve()
            if (root / "tools" / "search").is_dir():
                return root
        return Path.cwd().resolve()

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            if name == "trace_files":
                return self._trace_files()
            if name == "trace_all_search":
                return self._all_search(arguments)
            if name == "trace_search":
                return self._trace_search(arguments)
            if name == "trace_context":
                return self._trace_context(arguments)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        return f"Unknown tool: {name}"

    def close(self) -> None:
        self._close_search_daemon()

    def _ensure_search_bin(self) -> None:
        if self.search_bin.exists():
            return
        if not self.search_dir.is_dir():
            raise FileNotFoundError(
                f"Trace search tool directory not found: {self.search_dir}. "
                "Run algokiller from the repository root or set HARNESS_REPO_ROOT=/path/to/AlgoKiller."
            )
        subprocess.run(["make"], cwd=self.search_dir, check=True, capture_output=True, text=True)

    def _close_search_daemon(self) -> None:
        daemon = self.search_daemon
        self.search_daemon = None
        if daemon is None:
            return
        if daemon.poll() is None:
            try:
                if daemon.stdin is not None:
                    daemon.stdin.write("quit\n")
                    daemon.stdin.flush()
            except Exception:
                pass
            try:
                daemon.wait(timeout=1)
            except subprocess.TimeoutExpired:
                daemon.terminate()
                try:
                    daemon.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    daemon.kill()

    def _ensure_search_daemon(self) -> subprocess.Popen[str]:
        if self.search_daemon is not None and self.search_daemon.poll() is None:
            return self.search_daemon

        self._close_search_daemon()
        self._ensure_search_bin()
        trace_dir = self.trace_file if self.trace_file.is_dir() else self.trace_file.parent
        daemon = subprocess.Popen(
            [str(self.search_bin), "daemon", "--dir", str(trace_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        if daemon.stdout is None:
            daemon.kill()
            raise RuntimeError("Trace search daemon did not expose stdout")

        ready_line = daemon.stdout.readline()
        if not ready_line:
            stderr = daemon.stderr.read() if daemon.stderr is not None else ""
            daemon.wait(timeout=1)
            raise RuntimeError(f"Trace search daemon failed to start: {stderr.strip()}")
        try:
            ready = json.loads(ready_line)
        except json.JSONDecodeError as exc:
            daemon.kill()
            raise RuntimeError(f"Trace search daemon returned invalid ready message: {ready_line!r}") from exc
        if ready.get("type") != "daemon_ready" or ready.get("status") != "ok":
            daemon.kill()
            raise RuntimeError(f"Trace search daemon refused to start: {ready}")

        self.search_daemon = daemon
        return daemon

    def _daemon_request(self, command: str, *, max_output_chars: int = 30000, retry: bool = True) -> str:
        daemon = self._ensure_search_daemon()
        if daemon.stdin is None or daemon.stdout is None:
            self._close_search_daemon()
            raise RuntimeError("Trace search daemon pipes are unavailable")

        try:
            daemon.stdin.write(command + "\n")
            daemon.stdin.flush()
        except (BrokenPipeError, OSError):
            self._close_search_daemon()
            if retry:
                return self._daemon_request(command, max_output_chars=max_output_chars, retry=False)
            raise

        stdout_parts: list[str] = []
        stdout_chars = 0
        truncated = False
        while True:
            line = daemon.stdout.readline()
            if not line:
                stderr = daemon.stderr.read() if daemon.stderr is not None else ""
                self._close_search_daemon()
                if retry:
                    return self._daemon_request(command, max_output_chars=max_output_chars, retry=False)
                raise RuntimeError(f"Trace search daemon exited unexpectedly: {stderr.strip()}")
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and data.get("type") == "daemon_end":
                return json.dumps(
                    {
                        "status": "ok" if data.get("status") == "ok" else "error",
                        "returncode": 0 if data.get("status") == "ok" else 1,
                        "stdout": "".join(stdout_parts),
                        "stderr": str(data.get("error") or ""),
                        "truncated": truncated,
                    },
                    ensure_ascii=False,
                )
            if stdout_chars + len(line) <= max_output_chars:
                stdout_parts.append(line)
                stdout_chars += len(line)
            else:
                truncated = True

    def _run(self, argv: list[str], *, cwd: Path | None = None, max_output_chars: int = 20000) -> str:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        truncated = False
        if len(stdout) > max_output_chars:
            stdout = stdout[:max_output_chars]
            truncated = True
        return json.dumps(
            {
                "status": "ok" if completed.returncode == 0 else "error",
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr[-4000:],
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    def _positive_int(self, value: Any, default: int, name: str) -> int:
        if value is None:
            return default
        result = int(value)
        if result < 1:
            raise ValueError(f"{name} must be >= 1")
        return result

    def _required_positive_int(self, arguments: dict[str, Any], name: str, maximum: int | None = None) -> int:
        if name not in arguments:
            raise ValueError(f"{name} is required")
        result = self._positive_int(arguments.get(name), 1, name)
        if maximum is not None and result > maximum:
            raise ValueError(f"{name} must be <= {maximum}")
        return result

    def _non_negative_int(self, value: Any, default: int, name: str) -> int:
        if value is None:
            return default
        result = int(value)
        if result < 0:
            raise ValueError(f"{name} must be >= 0")
        return result

    def _bounded_non_negative_int(self, value: Any, default: int, name: str, maximum: int) -> int:
        result = self._non_negative_int(value, default, name)
        if result > maximum:
            raise ValueError(f"{name} must be <= {maximum}")
        return result

    def _trace_files(self) -> str:
        return self._daemon_request("list", max_output_chars=30000)

    def _trace_search_once(
        self,
        query: str,
        *,
        file_id: str,
        from_line: int = 0,
        before_line: int = 0,
        limit: int,
    ) -> str:
        query_hex = query.encode("utf-8").hex()
        return self._daemon_request(
            f"match\t{file_id}\t{from_line}\t{before_line}\t{limit}\t{query_hex}",
            max_output_chars=30000,
        )

    def _all_search_once(self, query: str, *, limit: int) -> str:
        query_hex = query.encode("utf-8").hex()
        return self._daemon_request(
            f"trace_all_search\t{limit}\t{query_hex}",
            max_output_chars=30000,
        )

    def _search_result_has_matches(self, result_json: str) -> bool:
        result = json.loads(result_json)
        return result.get("status") == "ok" and bool(str(result.get("stdout") or "").strip())

    def _search_result_is_empty_success(self, result_json: str) -> bool:
        result = json.loads(result_json)
        return result.get("status") == "ok" and not str(result.get("stdout") or "").strip()

    def _byte_reverse_hex_query(self, hex_digits: str) -> str:
        padded_hex = hex_digits if len(hex_digits) % 2 == 0 else "0" + hex_digits
        return "0x" + "".join(reversed([padded_hex[i : i + 2] for i in range(0, len(padded_hex), 2)]))

    def _hex_search_fallback_queries(self, query: str) -> list[str]:
        if not query.lower().startswith("0x"):
            return []

        hex_digits = query[2:]
        if not re.fullmatch(r"[0-9a-fA-F]+", hex_digits):
            return []

        fallbacks = [self._byte_reverse_hex_query(hex_digits)]

        trimmed_hex = hex_digits.lstrip("0")
        if trimmed_hex and trimmed_hex != hex_digits:
            fallbacks.append("0x" + trimmed_hex)
            fallbacks.append(self._byte_reverse_hex_query(trimmed_hex))

        unique_fallbacks: list[str] = []
        seen = {query.lower()}
        for fallback_query in fallbacks:
            normalized = fallback_query.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_fallbacks.append(fallback_query)
        return unique_fallbacks

    def _trace_search(self, arguments: dict[str, Any]) -> str:
        query = str(arguments["query"])
        if not query:
            raise ValueError("query must not be empty")
        if "file_id" not in arguments:
            raise ValueError("file_id is required")
        file_id_int = self._positive_int(arguments["file_id"], 1, "file_id")
        file_id = str(file_id_int)
        has_from_line = "from_line" in arguments
        has_before_line = "before_line" in arguments
        if has_from_line == has_before_line:
            raise ValueError("exactly one of from_line or before_line is required")
        limit = self._required_positive_int(arguments, "limit", 100)
        if has_before_line:
            before_line = self._required_positive_int(arguments, "before_line")
            result = self._trace_search_once(query, file_id=file_id, before_line=before_line, limit=limit)
            if not self._search_result_is_empty_success(result):
                return result
            for fallback_query in self._hex_search_fallback_queries(query):
                fallback_result = self._trace_search_once(
                    fallback_query,
                    file_id=file_id,
                    before_line=before_line,
                    limit=limit,
                )
                if self._search_result_has_matches(fallback_result):
                    return fallback_result
            return result

        from_line = self._required_positive_int(arguments, "from_line")
        result = self._trace_search_once(query, file_id=file_id, from_line=from_line, limit=limit)
        if not self._search_result_is_empty_success(result):
            return result
        for fallback_query in self._hex_search_fallback_queries(query):
            fallback_result = self._trace_search_once(
                fallback_query,
                file_id=file_id,
                from_line=from_line,
                limit=limit,
            )
            if self._search_result_has_matches(fallback_result):
                return fallback_result
        return result

    def _all_search(self, arguments: dict[str, Any]) -> str:
        extra_args = set(arguments) - {"query", "limit"}
        if extra_args:
            raise ValueError(f"trace_all_search only supports query and limit; unexpected: {', '.join(sorted(extra_args))}")
        query = str(arguments["query"])
        if not query:
            raise ValueError("query must not be empty")
        limit = self._required_positive_int(arguments, "limit", 10)
        result = self._all_search_once(query, limit=limit)
        if not self._search_result_is_empty_success(result):
            return result
        for fallback_query in self._hex_search_fallback_queries(query):
            fallback_result = self._all_search_once(fallback_query, limit=limit)
            if self._search_result_has_matches(fallback_result):
                return fallback_result
        return result

    def _trace_context(self, arguments: dict[str, Any]) -> str:
        if "context" in arguments:
            raise ValueError("context is no longer supported; use before and after")
        file_id = self._required_positive_int(arguments, "file_id")
        line = self._required_positive_int(arguments, "line")
        if "before" not in arguments:
            raise ValueError("before is required")
        if "after" not in arguments:
            raise ValueError("after is required")
        before_count = self._bounded_non_negative_int(arguments.get("before"), 0, "before", 100)
        after_count = self._bounded_non_negative_int(arguments.get("after"), 0, "after", 100)
        return self._daemon_request(
            f"context\t{file_id}\t{line}\t{before_count}\t{after_count}",
            max_output_chars=30000,
        )

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SEARCH_DIR = REPO_ROOT / "tools" / "search"
SEARCH_BIN = SEARCH_DIR / "ak_search"


def build_search_binary() -> None:
    subprocess.run(["make"], cwd=SEARCH_DIR, check=True, capture_output=True, text=True)


def run_ak_search(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    build_search_binary()
    return subprocess.run(
        [str(SEARCH_BIN), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_jsonl(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_search_selftest_covers_bitmap_filter_and_prefix_crop() -> None:
    completed = run_ak_search("selftest")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["search_threads"] >= 1


def test_match_excludes_instruction_prefix_before_bang(tmp_path: Path) -> None:
    trace = tmp_path / "trace.log"
    trace.write_text(
        "[lib.so] 0xaaa!0xbbb body_target\n"
        "plain prefix_target\n",
        encoding="utf-8",
    )

    prefix = run_ak_search("match", "--file", str(trace), "--query", "0xaaa", "--from-line", "1", "--limit", "10")
    body = run_ak_search("match", "--file", str(trace), "--query", "0xbbb", "--from-line", "1", "--limit", "10")

    assert prefix.returncode == 0, prefix.stderr
    assert prefix.stdout == ""
    assert body.returncode == 0, body.stderr
    assert [item["line"] for item in parse_jsonl(body.stdout)] == [1]


def test_daemon_parallel_search_preserves_forward_backward_order_and_context(tmp_path: Path) -> None:
    trace = tmp_path / "trace.log"
    trace.write_text(
        "\n".join(
            [
                "[lib.so] 0x111!target one",
                "plain target two",
                "[lib.so] 0x222!no hit",
                "plain target three",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    query_hex = "target".encode().hex()
    command = (
        f"match\t1\t0\t10\t{query_hex}\n"
        f"match\t0\t5\t10\t{query_hex}\n"
        "context\t1\t0\t0\n"
        "quit\n"
    )

    completed = run_ak_search("daemon", "--file", str(trace), input_text=command)

    assert completed.returncode == 0, completed.stderr
    rows = parse_jsonl(completed.stdout)
    assert rows[0]["type"] == "daemon_ready"
    assert rows[0]["search_threads"] >= 1

    first_end = next(i for i, row in enumerate(rows) if row.get("type") == "daemon_end")
    forward = rows[1:first_end]
    second_end = next(i for i, row in enumerate(rows[first_end + 1 :], start=first_end + 1) if row.get("type") == "daemon_end")
    backward = rows[first_end + 1 : second_end]
    context = rows[second_end + 1 : -1]

    assert [row["line"] for row in forward] == [1, 2, 4]
    assert [row["line"] for row in backward] == [4, 2, 1]
    assert context == [
        {
            "type": "context",
            "line": 1,
            "byte_offset": 0,
            "target": True,
            "text": "[lib.so] 0x111!target one",
        }
    ]

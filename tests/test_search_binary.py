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


def test_daemon_directory_file_list_search_and_context(tmp_path: Path) -> None:
    small = tmp_path / "small.log"
    small.write_text("small only\n", encoding="utf-8")
    large = tmp_path / "large.log"
    large.write_text(
        "\n".join(
            [
                "[lib.so] 0x111!0xbbb target one",
                "plain target two",
                "[lib.so] 0x222!no hit",
                "plain target three",
                "padding",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    query_hex = "target".encode().hex()
    prefix_hex = "0x111".encode().hex()
    command = (
        "list\n"
        f"match\t1\t1\t0\t10\t{prefix_hex}\n"
        f"match\t1\t1\t0\t10\t{query_hex}\n"
        f"trace_all_search\t10\t{query_hex}\n"
        "context\t1\t1\t0\t0\n"
        "quit\n"
    )

    completed = run_ak_search("daemon", "--dir", str(tmp_path), input_text=command)

    assert completed.returncode == 0, completed.stderr
    rows = parse_jsonl(completed.stdout)
    assert rows[0]["type"] == "daemon_ready"
    assert rows[0]["file_count"] == 2
    assert rows[0]["search_threads"] >= 1

    first_end = next(i for i, row in enumerate(rows) if row.get("type") == "daemon_end")
    file_list = rows[1:first_end]
    assert [item["file_id"] for item in file_list] == [1, 2]
    assert [set(item) for item in file_list] == [
        {"type", "file_id", "size_mb", "line_count"},
        {"type", "file_id", "size_mb", "line_count"},
    ]
    assert file_list[0]["line_count"] > file_list[1]["line_count"]

    second_end = next(i for i, row in enumerate(rows[first_end + 1 :], start=first_end + 1) if row.get("type") == "daemon_end")
    prefix = rows[first_end + 1 : second_end]

    third_end = next(i for i, row in enumerate(rows[second_end + 1 :], start=second_end + 1) if row.get("type") == "daemon_end")
    single = rows[second_end + 1 : third_end]

    fourth_end = next(i for i, row in enumerate(rows[third_end + 1 :], start=third_end + 1) if row.get("type") == "daemon_end")
    all_matches = rows[third_end + 1 : fourth_end]

    context = rows[fourth_end + 1 : -1]

    assert prefix == []
    assert [(row["file_id"], row["line"]) for row in single] == [(1, 1), (1, 2), (1, 4)]
    assert [(row["file_id"], row["line"]) for row in all_matches] == [(1, 1), (1, 2), (1, 4)]
    assert context == [
        {
            "type": "context",
            "file_id": 1,
            "line": 1,
            "byte_offset": 0,
            "target": True,
            "text": "[lib.so] 0x111!0xbbb target one",
        }
    ]


def test_trace_all_search_assigns_more_files_than_worker_threads(tmp_path: Path) -> None:
    for i in range(12):
        path = tmp_path / f"trace_{i:02d}.log"
        path.write_text(f"file {i} needle\npadding {i}\n", encoding="utf-8")
    query_hex = "needle".encode().hex()

    completed = run_ak_search(
        "daemon",
        "--dir",
        str(tmp_path),
        input_text=f"trace_all_search\t1\t{query_hex}\nquit\n",
    )

    assert completed.returncode == 0, completed.stderr
    rows = parse_jsonl(completed.stdout)
    ready = rows[0]
    assert ready["type"] == "daemon_ready"
    assert ready["file_count"] == 12
    assert ready["file_count"] > ready["search_threads"]

    matches = [row for row in rows if row.get("type") == "match"]
    assert len(matches) == 12
    assert sorted(row["file_id"] for row in matches) == list(range(1, 13))
    assert all(row["line"] == 1 for row in matches)


def test_trace_all_search_limit_is_per_file_and_bounded(tmp_path: Path) -> None:
    for file_index in range(2):
        path = tmp_path / f"trace_{file_index}.log"
        path.write_text(
            "\n".join(f"needle {file_index}-{line}" for line in range(3)) + "\n",
            encoding="utf-8",
        )
    query_hex = "needle".encode().hex()

    completed = run_ak_search(
        "daemon",
        "--dir",
        str(tmp_path),
        input_text=(
            f"trace_all_search\t2\t{query_hex}\n"
            f"trace_all_search\t11\t{query_hex}\n"
            f"match\tall\t1\t0\t10\t{query_hex}\n"
            "quit\n"
        ),
    )

    assert completed.returncode == 0, completed.stderr
    rows = parse_jsonl(completed.stdout)
    first_end = next(i for i, row in enumerate(rows) if row.get("type") == "daemon_end")
    matches = rows[1:first_end]
    assert len(matches) == 4
    assert {row["file_id"] for row in matches} == {1, 2}
    assert sum(1 for row in matches if row["file_id"] == 1) == 2
    assert sum(1 for row in matches if row["file_id"] == 2) == 2

    second_end = next(i for i, row in enumerate(rows[first_end + 1 :], start=first_end + 1) if row.get("type") == "daemon_end")
    assert rows[second_end]["status"] == "error"
    assert rows[second_end]["error"] == "trace_all_search limit must be between 1 and 10"

    third_end = next(i for i, row in enumerate(rows[second_end + 1 :], start=second_end + 1) if row.get("type") == "daemon_end")
    assert rows[third_end]["status"] == "error"
    assert rows[third_end]["error"] == "invalid file id"

# ak_search

High-throughput exact text search for very large trace-like files.

## Build

```bash
cd tools/search
make
```

## Exact Match

Return lines containing an exact string. Matching is ASCII case-insensitive: the query is folded to lowercase once, and file bytes are folded during scan without materializing a lowercase copy of the file. Line numbers are 1-based.

```bash
./ak_search match --file trace.log --query "x0=0x1234" --from-line 1000000 --limit 20
./ak_search match --file trace.log --query "mem_w=0x1234" --before-line 1000000 --limit 20
```

`--before-line` searches only lines before the anchor and returns nearest earlier
matches first. It is mutually exclusive with `--from-line`.

## Context

Return surrounding lines for a target line.

```bash
./ak_search context --file trace.log --line 1000000 --context 5
./ak_search context --file trace.log --line 1000000 --before 2 --after 8
```

Output is JSONL:

```json
{"type":"match","line":42,"byte_offset":8192,"text":"..."}
```

## Daemon

The harness uses daemon mode to mmap the trace and build a line-offset index once,
then reuse the same process for repeated `match` and `context` calls.

```bash
./ak_search daemon --file trace.log
```

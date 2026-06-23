# ak_search

High-throughput exact text search for very large trace-like files.

## Build

```bash
cd tools/search
make
```

## Exact Match

Return lines containing an exact string. Matching is ASCII case-insensitive:
the query is folded to lowercase once, and file bytes are folded during scan
without materializing a lowercase copy of the file. Line numbers are 1-based.

For instruction trace rows whose first byte is `[`, search ignores the prefix
through the first `!` character. The full row is still returned when the
post-`!` searchable region matches.

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

The harness uses daemon mode to mmap the trace and build indexes once, then
reuse the same process for repeated `match` and `context` calls.

Daemon indexes:

- line number -> byte offset;
- 128-bit ASCII presence bitmap per searchable line region.

Each `match` first checks whether the line bitmap can contain all ASCII bytes in
the query, then falls back to line-local BMH only for candidate rows. The bitmap
subset check uses inline assembly on supported targets. Daemon mode starts a
persistent search worker pool sized to half the online CPU cores; workers are
reused across searches. `context` remains single-threaded.

```bash
./ak_search daemon --file trace.log
```

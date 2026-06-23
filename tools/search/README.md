# ak_search

High-throughput exact text search for very large trace-like files.

## Build

```bash
cd tools/search
make
```

## Daemon

`ak_search` now runs as a directory-level daemon. Pass a directory; every regular
file ending in `.log` is opened, indexed, and assigned a file id by descending
file size. `file_id=1` is the largest file.

```bash
./ak_search daemon --dir /path/to/traces
```

Daemon protocol is tab-separated on stdin:

```bash
list
match FILE_ID FROM_LINE BEFORE_LINE LIMIT QUERY_HEX
trace_all_search LIMIT QUERY_HEX
context FILE_ID LINE BEFORE AFTER
quit
```

`match` searches one numeric file id and supports either `FROM_LINE` or
`BEFORE_LINE`. `trace_all_search` searches every opened file from line 1; `LIMIT` is
the maximum number of matches returned per file and must be between 1 and 10.
Every match row includes its source `file_id`.

Matching is ASCII case-insensitive. For instruction trace rows whose first byte
is `[`, search ignores the prefix through the first `!` character. The full row
is still returned when the post-`!` searchable region matches.

Output is JSONL:

```json
{"type":"file","file_id":1,"size_mb":16384.000,"line_count":100}
{"type":"match","file_id":1,"line":42,"byte_offset":8192,"text":"..."}
{"type":"context","file_id":1,"line":42,"byte_offset":8192,"target":true,"text":"..."}
{"type":"daemon_end","status":"ok"}
```

## Indexing and threading

Each file gets:

- line number -> byte offset;
- 128-bit ASCII presence bitmap per searchable line region.

Each `match` first checks whether the line bitmap can contain all ASCII bytes in
the query, then falls back to line-local BMH only for candidate rows. The bitmap
subset check uses inline assembly on supported targets. There is no intra-file
parallel search; the persistent worker pool is used only for `trace_all_search`
multi-file searches. `context` remains single-threaded.

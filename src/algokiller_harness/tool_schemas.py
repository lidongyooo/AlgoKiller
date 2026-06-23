from __future__ import annotations


RECOVERED_SOURCE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_recovered_source",
        "description": (
            "Write the final reconstructed Python source code for the user's task. "
            "Use this only when the recovered implementation is ready to deliver. "
            "Pass a stable relative .py path; the local harness automatically appends "
            "the current mode and datetime to the filename before writing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative .py path under the artifacts directory, for example recovered.py. "
                        "Do not add a mode or timestamp yourself; the harness adds them locally."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": "Complete Python source code.",
                },
                "notes": {
                    "type": "string",
                    "description": "Short evidence/confidence note to store next to the source.",
                },
            },
            "required": ["path", "source"],
        },
    },
}


ASK_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user a detailed clarification question only when the current analysis mode "
            "allows it and the target itself is ambiguous or missing. Do not use this just because "
            "optional context, field names, samples, or semantic labels are unavailable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The concrete question to ask the user.",
                },
                "why_needed": {
                    "type": "string",
                    "description": "Why this answer is required to recover the Python source correctly.",
                },
                "needed_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific function, register, memory address, sample, value, or context needed.",
                },
            },
            "required": ["question", "why_needed"],
        },
    },
}


TRACE_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "trace_search",
        "description": (
            "Case-insensitive exact substring search over indexed session trace files. "
            "Use this first to locate functions, registers, addresses, constants, call summaries, "
            "and hexdump ASCII text in very large traces. Every call must include file_id and exactly one "
            "of from_line or before_line, plus limit. before_line searches backward and returns "
            "nearest earlier matches first. file_id must be an integer file number from trace_files. "
            "Use trace_all_search for cross-file discovery. Choose a search purpose before each call. For byte/hex "
            "data starting with 0x, if the original query has no matches the harness automatically "
            "tries byte-reversed endian order; if that misses and the hex value has leading zeroes, "
            "it then tries the leading-zero-trimmed value and the byte-reversed trimmed value. "
            "Fallback hits are returned as normal search matches without extra annotations. For "
            "values longer than 4 bytes, try 2-4 distinctive 4-byte windows in both original and "
            "reversed order before expanding. limit must be no greater than 100."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "Required trace file number from trace_files.",
                    "minimum": 1,
                },
                "query": {
                    "type": "string",
                    "description": "Exact substring to find. Matching is case-insensitive for ASCII.",
                },
                "from_line": {
                    "type": "integer",
                    "description": "Required 1-based file line to start searching from.",
                    "minimum": 1,
                },
                "before_line": {
                    "type": "integer",
                    "description": (
                        "Required instead of from_line when searching backward. "
                        "Only lines before this 1-based file line are searched; nearest matches are returned first."
                    ),
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Required maximum number of matching lines to return. Must be <= 100.",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["file_id", "query", "limit"],
        },
    },
}


TRACE_ALL_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "trace_all_search",
        "description": (
            "Forward case-insensitive exact substring search across every opened .log trace file. "
            "Only use this for cross-file discovery when the relevant file is unknown. Every returned "
            "match includes its source file_id. limit is the maximum number of records to return per file "
            "and must be between 1 and 10."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Exact substring to find in every opened file. Matching is case-insensitive for ASCII.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Required per-file maximum number of matching lines to return. Must be between 1 and 10.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query", "limit"],
        },
    },
}


TRACE_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "trace_context",
        "description": (
            "Return neighboring trace text lines around a 1-based line in a numbered session trace file. "
            "Use this after trace_search to inspect instruction, call, ret, and hexdump context. "
            "Every call must include file_id plus explicit before and after line counts; each line-count "
            "argument must be no greater than 100."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "integer",
                    "description": "Required trace file number from trace_files.",
                    "minimum": 1,
                },
                "line": {
                    "type": "integer",
                    "description": "1-based target file line.",
                },
                "before": {
                    "type": "integer",
                    "description": "Required explicit number of lines before the target. Must be <= 100.",
                    "minimum": 0,
                    "maximum": 100,
                },
                "after": {
                    "type": "integer",
                    "description": "Required explicit number of lines after the target. Must be <= 100.",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            "required": ["file_id", "line", "before", "after"],
        },
    },
}


TRACE_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "trace_files",
        "description": (
            "List opened .log trace files with their numeric file_id, size in MB, and line count. "
            "Use this before trace_search/trace_context when file numbering is unknown."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

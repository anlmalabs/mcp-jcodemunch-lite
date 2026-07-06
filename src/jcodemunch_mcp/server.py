"""MCP server for jcodemunch-mcp."""

import argparse
import asyncio
import atexit
import errno
import functools
import hmac
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any, Optional

# IO type for text file handles
TextIO = IO[str]

import jsonschema
from mcp.server import Server
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    Prompt,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)

from . import __version__
from . import config as config_module

# Tool modules are imported lazily inside each call_tool() dispatch branch.
# This defers loading heavy dependencies (tree-sitter, httpx, pathspec) until
# the first actual call to a tool that needs them, reducing cold-start latency
# for sessions that only use query tools and never trigger indexing.
from .parser.symbols import VALID_KINDS
from .reindex_state import await_freshness_if_strict
from .storage import result_cache_invalidate as _result_cache_invalidate
from .storage import write_pulse as _write_pulse
from .summarizer import get_provider_name

try:
    from .watcher import WatcherError, WatcherManager, watch_folders
except ImportError:
    watch_folders = None  # type: ignore[assignment, misc]
    WatcherManager = None  # type: ignore[assignment, misc]
    WatcherError = type("WatcherError", (Exception,), {})  # type: ignore[assignment, misc]

# Global watcher manager instance (set in _run_server_with_watcher)
_watcher_manager: Optional["WatcherManager"] = None


# Canonical list of all registered tool names (unfiltered).
# Keep in sync with _build_tools_list(). Used by `config --check` and
# `claude-md --generate` to detect CLAUDE.md / hook-script drift.
_CANONICAL_TOOL_NAMES: tuple[str, ...] = (
    # Indexing
    "index_content",  # replaces index_repo + index_folder
    "summarize_repo",
    "index_file",
    # Discovery
    "list_repos",
    "resolve_repo",
    "list_content",  # replaces get_file_tree
    "get_outline",  # replaces get_file_outline
    # Search & Retrieval
    "search_units",  # replaces search_symbols
    "get_unit",  # replaces get_symbol_source
    "get_unit_context",  # replaces get_context_bundle
    "get_file",  # replaces get_file_content
    "search_text",
    "search_columns",
    "assemble_task_context",
    # Relationships
    "find_references",
    "get_dependency_graph",
    "get_class_hierarchy",
    "get_call_hierarchy",
    # Impact & Safety
    "get_blast_radius",
    "check_safe",
    "get_changed_symbols",
    "plan_refactoring",
    "get_symbol_provenance",
    "get_pr_risk_profile",
    # Symbol navigation
    "find_implementations",
    # Architecture
    "get_tectonic_map",
    "get_project_intel",
    "list_workspaces",
    # Quality & Metrics
    "get_symbol_complexity",
    "get_repo_health",
    "get_repo_map",
    "get_dead_code_v2",
    "find_similar_symbols",
    "search_ast",
    "embed_repo",
    "register_edit",
    # Agent stand-up briefing
    # Composite retrieval
    # Unified Reading (code + docs)
    # Runtime trace ingest + analytics
    "import_runtime_signal",
    "find_hot_paths",
)

# Category groupings for the generated CLAUDE.md snippet (`claude-md --generate`).
# Module-level so test_tool_registration_consistency can enumerate it: every
# tool the builder emits must appear here AND in _CANONICAL_TOOL_NAMES, or the
# meta-test fails listing the gap. Keeps a new tool from drifting across the
# registration surfaces (the recurring "added the tool in 4 of 5 places" trap).
_SNIPPET_TOOL_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Indexing", ["index_content", "summarize_repo", "index_file"]),
    (
        "Discovery",
        ["list_repos", "resolve_repo", "list_content", "get_outline"],
    ),
    (
        "Search & Retrieval",
        [
            "search_units",
            "get_unit",
            "get_unit_context",
            "get_file",
            "search_text",
            "search_columns",
            "assemble_task_context",
        ],
    ),
    (
        "Relationships",
        [
            "find_references",
            "get_dependency_graph",
            "get_class_hierarchy",
            "get_call_hierarchy",
            "find_implementations",
        ],
    ),
    (
        "Impact & Safety",
        [
            "get_blast_radius",
            "check_safe",
            "get_changed_symbols",
            "plan_refactoring",
            "get_symbol_provenance",
            "get_pr_risk_profile",
        ],
    ),
    (
        "Architecture",
        ["get_tectonic_map", "get_project_intel", "list_workspaces"],
    ),
    (
        "Quality & Metrics",
        [
            "get_symbol_complexity",
            "get_repo_health",
            "get_repo_map",
            "find_similar_symbols",
            "get_dead_code_v2",
            "search_ast",
        ],
    ),
    ("Diffs & Embeddings", ["embed_repo"]),
    ("Session", ["register_edit"]),
    ("Runtime Trace Ingest & Analytics", ["import_runtime_signal", "find_hot_paths"]),
]


# Hidden legacy reading names that still dispatch for compatibility. Treat
# these as the canonical replacement for config disabling so old configs keep
# working and new configs cannot bypass a disabled unified tool via an alias.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    "index_repo": "index_content",
    "index_folder": "index_content",
    "get_file_tree": "list_content",
    "get_file_outline": "get_outline",
    "get_file_content": "get_file",
    "search_symbols": "search_units",
    "get_symbol_source": "get_unit",
    "get_context_bundle": "get_unit_context",
}
_CANONICAL_TO_ALIASES: dict[str, set[str]] = {}
for _alias, _canonical in _ALIAS_TO_CANONICAL.items():
    _CANONICAL_TO_ALIASES.setdefault(_canonical, set()).add(_alias)


def _tool_disable_names(name: str) -> set[str]:
    canonical = _ALIAS_TO_CANONICAL.get(name, name)
    return {name, canonical, *_CANONICAL_TO_ALIASES.get(canonical, set())}


def _disabled_tool_match(name: str, repo: str | None = None) -> str | None:
    for candidate in _tool_disable_names(name):
        if config_module.is_tool_disabled(candidate, repo=repo):
            return candidate
    return None


def _disabled_by_set(name: str, disabled: set[str]) -> bool:
    return bool(_tool_disable_names(name) & disabled)


# --- The Counter: adaptive tool surface (front door) ----------------------- #
# order/menu/route collapse the ~83-tool surface to a 3-tool front door without
# removing any capability. See docs/prd-adaptive-tool-surface.md + counter.py.
from . import counter as _counter

_COUNTER_FRONT_DOOR: frozenset[str] = _counter.FRONT_DOOR

# Unfiltered tool catalog, captured by _build_tools_list before surface
# filtering. Single source of truth for menu() and order()'s action allowlist,
# so the front door can surface/dispatch any action regardless of surface mode.
_RAW_CATALOG: "Optional[list]" = None


def _effective_surface() -> str:
    """Active tool surface. 'counter' collapses list_tools to the front door;
    anything else (default 'full') lists all tools.
    Env JCODEMUNCH_TOOL_SURFACE wins over config 'tool_surface'.
    """
    env = os.environ.get("JCODEMUNCH_TOOL_SURFACE")
    if env:
        return env.strip().lower()
    return (config_module.get("tool_surface", "full") or "full").strip().lower()


def _counter_front_door_tools() -> list:
    """Tool definitions for order / menu / route."""
    return [
        Tool(
            name="order",
            description=(
                "Dispatch any jcodemunch action by name: order(action, args). The "
                "single-verb front door to the full tool catalog. Read-only by "
                "default — actions that change index/session state require "
                "allow_state_change=true, and execution/file-write verbs are refused. "
                "Call 'menu' to discover actions, or 'route' to pick one from a task."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Name of the catalog action to run (e.g. 'search_symbols').",
                    },
                    "args": {
                        "type": "object",
                        "description": "Arguments for that action, exactly as you'd pass them directly.",
                        "default": {},
                    },
                    "allow_state_change": {
                        "type": "boolean",
                        "description": "Opt in to dispatching an index/session state-changing action (e.g. index_repo).",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="menu",
            description=(
                "Discover catalog actions without keeping all ~83 tool schemas "
                "resident: menu(query?). Returns compact rows (action, summary, "
                "required args, state_changing). With no query, lists the catalog. "
                "Pair with 'order' to dispatch the chosen action."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional. Keywords describing what you want to do; ranks matching actions.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max actions to return.",
                        "default": 25,
                    },
                },
            },
        ),
        Tool(
            name="route",
            description=(
                "Map a natural-language task to the best catalog action(s): "
                "route(task, repo?, execute?). Returns ranked recommendations with "
                "ready-to-run argument templates. With execute=true, dispatches the "
                "top recommendation and returns its result in the same call, "
                "collapsing discover-then-call into one round-trip. Recommends "
                "assemble_task_context for context-gathering intents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What you're trying to do, in plain language.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (required to execute repo-scoped actions).",
                    },
                    "execute": {
                        "type": "boolean",
                        "description": "If true, dispatch the top recommended action and return its result.",
                        "default": False,
                    },
                },
                "required": ["task"],
            },
        ),
    ]


def _reading_surface_tools() -> list:
    """Unified jMRI-style tools backed by code and doc indexes."""
    domain_prop = {
        "type": "string",
        "enum": ["auto", "both", "code", "docs"],
        "description": "Which index domain to query. auto routes by id/path when possible.",
        "default": "auto",
    }
    return [
        Tool(
            name="index_content",
            description="Index code, docs, or both from a local path or GitHub URL. Docs are handled by the integrated jDocMunch engine.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local folder or file path.",
                    },
                    "url": {
                        "type": "string",
                        "description": "GitHub repository URL or owner/repo.",
                    },
                    "domain": {**domain_prop, "default": "both"},
                    "use_ai_summaries": {"type": "boolean", "default": True},
                    "use_embeddings": {
                        "description": "Docs embedding mode passed to jDocMunch.",
                        "default": "auto",
                    },
                    "incremental": {"type": "boolean", "default": True},
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional explicit paths relative to path.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional docs storage name for jDocMunch local/GitHub indexes.",
                    },
                    "extra_ignore_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional gitignore-style patterns to exclude from indexing.",
                    },
                    "follow_symlinks": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include symlinked files. Symlinked directories are never followed.",
                    },
                    "identity_mode": {
                        "type": "string",
                        "enum": ["config", "local", "git"],
                        "description": "Repo-identity strategy. config (default): respect existing index. local: path-keyed. git: git-root-keyed (monorepo subdir merging).",
                        "default": "config",
                    },
                },
            },
        ),
        Tool(
            name="list_content",
            description="List indexed code files and/or documentation files/TOC without reading bodies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Code or docs repo handle.",
                    },
                    "domain": {**domain_prop, "default": "both"},
                    "path_prefix": {"type": "string", "default": ""},
                    "path_glob": {
                        "type": "string",
                        "description": "Docs path glob, e.g. docs/api/**.",
                    },
                    "tree": {"type": "boolean", "default": False},
                    "include_summaries": {"type": "boolean", "default": False},
                    "max_files": {"type": "integer"},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_outline",
            description="Get a code symbol outline or documentation section outline for a file path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "file_path": {"type": "string"},
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to query in batch mode. Returns a grouped results array.",
                    },
                    "domain": domain_prop,
                },
                "required": ["repo"],
                "anyOf": [
                    {"required": ["file_path"]},
                    {"required": ["file_paths"]},
                ],
            },
        ),
        Tool(
            name="get_file",
            description="Read cached code source or cached preprocessed document content by file path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "file_path": {"type": "string"},
                    "domain": domain_prop,
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["repo", "file_path"],
            },
        ),
        Tool(
            name="search_units",
            description="Search semantic units: code symbols and/or documentation sections.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repo handle. Required for code; optional for docs repo_group fan-out in future.",
                    },
                    "query": {"type": "string"},
                    "domain": {**domain_prop, "default": "both"},
                    "file_path": {
                        "type": "string",
                        "description": "Exact file/doc path scope.",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Code file glob scope.",
                    },
                    "path_glob": {
                        "type": "string",
                        "description": "Glob scope for files/docs.",
                    },
                    "max_results": {"type": "integer", "default": 10},
                    "mode": {
                        "type": "string",
                        "enum": ["default", "title"],
                        "default": "default",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Optional filter by symbol kind",
                        "enum": [
                            "function",
                            "class",
                            "method",
                            "constant",
                            "type",
                            "template",
                            "import",
                        ],
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional filter by language",
                        "enum": _build_language_enum(),
                    },
                    "decorator": {
                        "type": "string",
                        "description": "Optional filter: only return symbols with this decorator (case-insensitive substring match, e.g. 'route', 'property', 'Deprecated')",
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "Token budget cap. When set, results are sorted by score and greedily packed until the budget is exhausted.",
                    },
                    "detail_level": {
                        "type": "string",
                        "description": "Controls result verbosity. 'compact' returns id/name/kind/file/line only. 'standard' returns signatures and summaries (default). 'full' inlines source code, docstring, and end_line.",
                        "enum": ["compact", "standard", "full"],
                        "default": "standard",
                    },
                    "debug": {
                        "type": "boolean",
                        "description": "When true, each result includes a score_breakdown showing per-field scoring contributions.",
                        "default": False,
                    },
                    "fuzzy": {
                        "type": "boolean",
                        "description": "Enable fuzzy matching. When true, uses trigram overlap + Levenshtein distance as fallback when BM25 scores are low.",
                        "default": False,
                    },
                    "fuzzy_threshold": {
                        "type": "number",
                        "description": "Minimum Jaccard trigram similarity (0.0–1.0) for fuzzy candidates.",
                        "default": 0.4,
                    },
                    "max_edit_distance": {
                        "type": "integer",
                        "description": "Maximum Levenshtein distance for direct name matching (catches typos).",
                        "default": 2,
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["relevance", "centrality", "combined"],
                        "description": "Ranking strategy. 'relevance' (default) = BM25 text match. 'centrality' = filter by query, rank by PageRank. 'combined' = BM25 + PageRank weighted.",
                        "default": "relevance",
                    },
                    "semantic": {
                        "type": "boolean",
                        "description": "Enable semantic (embedding-based) search. Requires an embedding provider.",
                        "default": False,
                    },
                    "semantic_weight": {
                        "type": "number",
                        "description": "Weight for semantic score in hybrid BM25+embedding ranking (0.0–1.0).",
                        "default": 0.5,
                    },
                    "semantic_only": {
                        "type": "boolean",
                        "description": "Skip BM25 entirely and rank solely by embedding cosine similarity.",
                        "default": False,
                    },
                    "fusion": {
                        "type": "boolean",
                        "description": "Enable multi-signal fusion (Weighted Reciprocal Rank) across lexical, structural, similarity, and identity channels.",
                        "default": False,
                    },
                    "fqn": {
                        "type": "string",
                        "description": "PHP fully-qualified class name (e.g. 'App\\Models\\User'). Resolves via PSR-4 and uses the class name as query.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_unit",
            description="Retrieve one or more units: code symbol source or documentation section content. Prefix ids with code: or doc: to force routing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "unit_id": {"type": "string"},
                    "unit_ids": {"type": "array", "items": {"type": "string"}},
                    "domain": domain_prop,
                    "verify": {"type": "boolean", "default": False},
                    "verify_against": {
                        "type": "string",
                        "enum": ["cache", "git_sha"],
                        "description": "Where to source the comparison target when verify=True. 'cache' (default) compares against the content_hash stored in the index. 'git_sha' additionally compares against the file slice at the working-tree git HEAD.",
                        "default": "cache",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Number of lines before/after symbol to include for context",
                        "default": 0,
                    },
                    "fqn": {
                        "type": "string",
                        "description": "PHP fully-qualified class name (e.g. 'App\\Models\\User'). Resolves to symbol_id via PSR-4. Alternative to unit_id.",
                    },
                    "source_start_line": {
                        "type": "integer",
                        "description": "Bounded mode: absolute file line (1-based, same frame as line/end_line) to start the returned source slice; clamped to the symbol body.",
                    },
                    "source_end_line": {
                        "type": "integer",
                        "description": "Bounded mode: absolute file line (1-based, inclusive) to end the returned source slice; clamped to the symbol body.",
                    },
                    "max_source_lines": {
                        "type": "integer",
                        "description": "Bounded mode: keep at most the first N lines of the (ranged) slice. Sets source_truncated + metadata when it shortens the body.",
                    },
                    "max_source_bytes": {
                        "type": "integer",
                        "description": "Bounded mode: UTF-8-safe per-symbol byte cap on the returned source. Verify still hashes the full body.",
                    },
                    "max_total_source_bytes": {
                        "type": "integer",
                        "description": "Bounded mode (batch): cap on total returned source bytes across all symbols. Oversized symbols come back partial rather than dropped.",
                    },
                    "strip_boilerplate": {"type": "boolean", "default": False},
                    "compress_code": {"type": "boolean", "default": False},
                },
                "required": ["repo"],
                "anyOf": [
                    {"required": ["unit_id"]},
                    {"required": ["unit_ids"]},
                    {"required": ["fqn"]},
                ],
            },
        ),
        Tool(
            name="get_unit_context",
            description="Retrieve surrounding context for a code symbol or documentation section.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "unit_id": {"type": "string"},
                    "domain": domain_prop,
                    "token_budget": {"type": "integer"},
                    "include_callers": {"type": "boolean", "default": False},
                    "output_format": {
                        "type": "string",
                        "enum": ["json", "markdown"],
                        "default": "json",
                        "description": "'json' (default) or 'markdown' — markdown renders a paste-ready document with imports, docstrings, and source blocks.",
                    },
                    "budget_strategy": {
                        "type": "string",
                        "enum": ["most_relevant", "core_first", "compact"],
                        "default": "most_relevant",
                        "description": "'most_relevant' (default) ranks by file centrality. 'core_first' keeps the primary symbol first. 'compact' strips source bodies — returns signatures only.",
                    },
                    "include_budget_report": {"type": "boolean", "default": False},
                    "fqn": {
                        "type": "string",
                        "description": "PHP fully-qualified class name (e.g. 'App\\Models\\User'). Resolves to unit_id via PSR-4. Alternative to unit_id.",
                    },
                    "strip_boilerplate": {"type": "boolean", "default": False},
                    "include_related": {"type": "boolean", "default": False},
                },
                "required": ["repo", "unit_id"],
            },
        ),
    ]


def _legacy_reading_tool_schemas() -> list:
    """Schemas for removed tool names that remain directly dispatchable."""
    return [
        Tool(
            name="index_repo",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "use_ai_summaries": {"type": "boolean", "default": True},
                    "incremental": {"type": "boolean", "default": True},
                    "extra_ignore_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="index_folder",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "use_ai_summaries": {"type": "boolean", "default": True},
                    "incremental": {"type": "boolean", "default": True},
                    "extra_ignore_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "follow_symlinks": {"type": "boolean", "default": False},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "identity_mode": {
                        "type": "string",
                        "enum": ["config", "local", "git"],
                        "default": "config",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="get_file_tree",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "path_prefix": {"type": "string", "default": ""},
                    "include_summaries": {"type": "boolean", "default": False},
                    "max_files": {"type": "integer"},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_file_outline",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "file_path": {"type": "string"},
                    "file": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["repo"],
                "anyOf": [
                    {"required": ["file_path"]},
                    {"required": ["file"]},
                    {"required": ["file_paths"]},
                ],
            },
        ),
        Tool(
            name="get_file_content",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["repo", "file_path"],
            },
        ),
        Tool(
            name="search_symbols",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "query": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(VALID_KINDS)},
                    "file_pattern": {"type": "string"},
                    "language": {"type": "string"},
                    "decorator": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10},
                    "token_budget": {"type": "integer"},
                    "detail_level": {
                        "type": "string",
                        "enum": ["compact", "standard", "full"],
                        "default": "standard",
                    },
                    "debug": {"type": "boolean", "default": False},
                    "fuzzy": {"type": "boolean", "default": False},
                    "fuzzy_threshold": {"type": "number", "default": 0.4},
                    "max_edit_distance": {"type": "integer", "default": 2},
                    "sort_by": {
                        "type": "string",
                        "enum": ["relevance", "centrality", "combined"],
                        "default": "relevance",
                    },
                    "semantic": {"type": "boolean", "default": False},
                    "semantic_weight": {"type": "number", "default": 0.5},
                    "semantic_only": {"type": "boolean", "default": False},
                    "fusion": {"type": "boolean", "default": False},
                    "fqn": {"type": "string"},
                },
                "required": ["repo", "query"],
            },
        ),
        Tool(
            name="get_symbol_source",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol_id": {"type": "string"},
                    "symbol_ids": {"type": "array", "items": {"type": "string"}},
                    "verify": {"type": "boolean", "default": False},
                    "verify_against": {
                        "type": "string",
                        "enum": ["cache", "git_sha"],
                        "default": "cache",
                    },
                    "context_lines": {"type": "integer", "default": 0},
                    "fqn": {"type": "string"},
                    "source_start_line": {"type": "integer"},
                    "source_end_line": {"type": "integer"},
                    "max_source_lines": {"type": "integer"},
                    "max_source_bytes": {"type": "integer"},
                    "max_total_source_bytes": {"type": "integer"},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_context_bundle",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol_id": {"type": "string"},
                    "symbol_ids": {"type": "array", "items": {"type": "string"}},
                    "include_callers": {"type": "boolean", "default": False},
                    "output_format": {
                        "type": "string",
                        "enum": ["json", "markdown"],
                        "default": "json",
                    },
                    "token_budget": {"type": "integer"},
                    "budget_strategy": {
                        "type": "string",
                        "enum": ["most_relevant", "core_first", "compact"],
                        "default": "most_relevant",
                    },
                    "include_budget_report": {"type": "boolean", "default": False},
                    "fqn": {"type": "string"},
                },
                "required": ["repo"],
                "anyOf": [
                    {"required": ["symbol_id"]},
                    {"required": ["symbol_ids"]},
                    {"required": ["fqn"]},
                ],
            },
        ),
    ]


def _raw_catalog_tools() -> list:
    """Return the unfiltered catalog, building it once on demand."""
    global _RAW_CATALOG
    if _RAW_CATALOG is None:
        _build_tools_list()  # populates _RAW_CATALOG as a side effect
    return _RAW_CATALOG or []


def _catalog_rows() -> "list[dict]":
    """Menu-shaped rows for every real action (front door excluded)."""
    rows = []
    for t in _raw_catalog_tools():
        if t.name in _COUNTER_FRONT_DOOR:
            continue
        row = _counter.catalog_entry(t.name, t.description or "", t.inputSchema or {})
        row["_description"] = t.description or ""
        rows.append(row)
    return rows


def _catalog_names() -> set:
    return {t.name for t in _raw_catalog_tools() if t.name not in _COUNTER_FRONT_DOOR}


async def _emit_tools_list_changed() -> None:
    """Send notifications/tools/list_changed to the client, best-effort.

    No-op if the transport / SDK does not support it.
    """
    session = _get_mcp_session(server)
    if session is None:
        logger.debug("tools/list_changed skipped: no active MCP session")
        return

    send_fn = getattr(session, "send_tool_list_changed", None)
    if send_fn is None:
        logger.warning(
            "tools/list_changed skipped: session has no send_tool_list_changed()"
        )
        return

    try:
        maybe_awaitable = send_fn()
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable
    except (RuntimeError, TypeError, AttributeError) as exc:
        logger.warning("tools/list_changed notification failed: %s", exc, exc_info=True)


def _get_mcp_session(mcp_server: Server | None = None) -> Any | None:
    """Best-effort session lookup from an MCP server instance.

    Returns None when no request context/session is available.
    """
    srv = mcp_server if mcp_server is not None else globals().get("server")
    if srv is None:
        return None
    try:
        request_context = srv.request_context
    except (LookupError, AttributeError):
        return None
    if request_context is None:
        return None
    return getattr(request_context, "session", None)


# Parameters stripped from tool schemas when compact_schemas is enabled.
# These are advanced/rarely-used params that cost tokens every session but
# are used <5% of the time.  The underlying handler still accepts them.
_COMPACT_STRIP_PARAMS: dict[str, set[str]] = {
    "search_units": {
        "debug",
        "fusion",
        "semantic",
        "semantic_only",
        "semantic_weight",
        "fuzzy",
        "fuzzy_threshold",
        "max_edit_distance",
        "sort_by",
        "fqn",
        "decorator",
        "token_budget",
    },
    # Bounded-source mode is an advanced opt-in; the tool still accepts these
    # params under compact, they're just hidden from the schema to protect the
    # core_compact budget (the body is always callable with them).
    "get_unit": {
        "source_start_line",
        "source_end_line",
        "max_source_lines",
        "max_source_bytes",
        "max_total_source_bytes",
    },
    "get_unit_context": {"budget_strategy"},
    "get_blast_radius": {"cross_repo", "max_depth"},
    "get_dependency_graph": {"cross_repo"},
    "index_content": {"extra_ignore_patterns", "incremental"},
}

# Params whose enum is demoted to a plain string filter under compact_schemas.
# The `language` enum is the full LANGUAGE_REGISTRY (~76 values) — ~200 tokens
# of mechanical names an agent already knows. Dropping the enum keeps the param
# fully usable as a free-string filter (the tool tolerates any language string)
# while reclaiming the tokens. Keyed by param name so every tool that exposes a
_COMPACT_DEMOTE_ENUM_PARAMS: frozenset[str] = frozenset({"language"})

# Tools eligible for Agent Selector complexity scoring
_AGENT_SELECTOR_TOOLS = frozenset(
    {
        "get_unit_context",  # replaces get_context_bundle
        "search_units",  # replaces search_symbols
        "search_text",
        "get_unit",  # replaces get_symbol_source
        "get_blast_radius",
        "get_call_hierarchy",
        "get_dependency_graph",
    }
)

# Tools excluded from strict freshness mode (don't wait for reindex)
_EXCLUDED_FROM_STRICT = frozenset(
    {
        "list_repos",
        "resolve_repo",
        "index_content",  # unified tool replaces index_repo + index_folder
        "index_repo",  # backward-compat alias
        "index_folder",  # backward-compat alias
        "index_file",
    }
)


logger = logging.getLogger(__name__)


def _default_use_ai_summaries() -> bool:
    """Return whether AI summarization is enabled, as a bool.

    Collapses the tri-state config value ("auto", True, "true" → True;
    "false", False, "0", "no", "off" → False) into a simple gate.
    Note: _create_summarizer() reads the config directly to resolve
    the "auto" vs. explicit-provider distinction at summarization time.
    """
    raw = config_module.get("use_ai_summaries", "auto")
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("false", "0", "no", "off")


def _load_index_paths_from_arg(paths_from: str) -> tuple[Optional[list], Optional[str]]:
    """Read explicit paths from a file or stdin for `jcodemunch-mcp index --paths-from`.

    Returns ``(paths, None)`` on success or ``(None, error_message)`` on failure.
    Filters out empty lines and ``# …`` comments. An empty list is treated as
    an error so the command doesn't silently fall through to a full-tree index.
    """
    from pathlib import Path as _Path

    try:
        if paths_from == "-":
            raw = sys.stdin.read()
        else:
            raw = _Path(paths_from).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"Cannot read --paths-from {paths_from!r}: {e}"
    out = [
        ln.strip()
        for ln in raw.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not out:
        return None, f"--paths-from {paths_from!r} contained no usable paths"
    return out, None


# ---------------------------------------------------------------------------
# Session state persistence (Feature 10: Session-Aware Routing)
# ---------------------------------------------------------------------------

_session_state_restored = False


def _restore_session_state() -> None:
    """Load and restore session state on server startup.

    Called from run_stdio_server / run_sse_server / run_streamable_http_server.
    Restores journal entries and search cache from previous session.
    """
    global _session_state_restored
    if _session_state_restored:
        return

    if not config_module.get("session_resume", False):
        return

    try:
        from .storage import SQLiteIndexStore
        from .tools.search_symbols import _result_cache, _result_cache_lock
        from .tools.session_journal import get_journal
        from .tools.session_state import get_session_state

        state = get_session_state()
        max_age = config_module.get("session_max_age_minutes", 30)

        loaded = state.load(max_age_minutes=max_age)
        if not loaded:
            logger.debug("No session state to restore")
            return

        # Restore journal
        journal = get_journal()
        count = state.restore_journal(journal, loaded)
        logger.info("Restored %d session journal entries", count)

        # Build current_indexes for cache restoration
        storage_path = os.environ.get("CODE_INDEX_PATH", "")
        store = SQLiteIndexStore(base_path=storage_path)
        current_indexes = {}
        try:
            repos = store.list_repos()
            for r in repos:
                # list_repos already returns indexed_at — no need to load full index
                repo_id = r.get("repo", f"{r.get('owner', '')}/{r.get('name', '')}")
                indexed_at = r.get("indexed_at", "")
                if indexed_at:
                    current_indexes[repo_id] = indexed_at
        except Exception:
            pass

        # Restore search cache
        with _result_cache_lock:
            count = state.restore_search_cache(_result_cache, loaded, current_indexes)
        logger.info("Restored %d search cache entries", count)

        _session_state_restored = True

    except Exception as e:
        logger.warning("Failed to restore session state: %s", e)


def _save_session_state() -> None:
    """Save session state on server shutdown.

    Registered with atexit for clean shutdown.
    """
    if not config_module.get("session_resume", False):
        return

    try:
        from .tools.search_symbols import _result_cache, _result_cache_lock
        from .tools.session_journal import get_journal
        from .tools.session_state import get_session_state

        state = get_session_state()
        journal = get_journal()
        max_queries = config_module.get("session_max_queries", 50)

        neg_log = journal.get_negative_evidence_log()
        with _result_cache_lock:
            state.save(
                journal,
                _result_cache,
                max_queries=max_queries,
                negative_evidence_log=neg_log,
            )

        logger.info("Saved session state")

    except Exception as e:
        logger.warning("Failed to save session state: %s", e)


# Register atexit handler for session state persistence
atexit.register(_save_session_state)


# ---------------------------------------------------------------------------
# Live journal persistence (#334) — feeds the out-of-process PreCompact hook.
#
# The hook (`jcodemunch-mcp hook-precompact`) runs in a separate process from
# this server, so it sees a fresh, empty SessionJournal. We persist a compact
# snapshot of the live journal to a small file the hook reads back. Writes are
# throttled (not every tool call) and best-effort. Disable with
# JCODEMUNCH_LIVE_JOURNAL=0.
# ---------------------------------------------------------------------------

_live_journal_lock = threading.Lock()
_live_journal_last_flush = 0.0
_LIVE_JOURNAL_MIN_INTERVAL_S = 2.0


def _live_journal_enabled() -> bool:
    val = os.environ.get("JCODEMUNCH_LIVE_JOURNAL", "").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _maybe_flush_live_journal(journal) -> None:
    """Throttled, best-effort flush of the live journal to disk (#334)."""
    if not _live_journal_enabled():
        return
    global _live_journal_last_flush
    now = time.monotonic()
    with _live_journal_lock:
        if now - _live_journal_last_flush < _LIVE_JOURNAL_MIN_INTERVAL_S:
            return
        _live_journal_last_flush = now
    try:
        from .tools.session_state import save_live_journal

        save_live_journal(journal, base_path=os.environ.get("CODE_INDEX_PATH") or None)
    except Exception:
        logger.debug("live journal flush failed", exc_info=True)


def _cleanup_mermaid_temp_startup() -> None:
    """Clean stale mermaid viewer temp files from previous sessions."""
    if not config_module.get("render_diagram_viewer_enabled", False):
        return
    try:
        from .tools.mermaid_viewer import cleanup_temp_dir

        cleanup_temp_dir()
    except Exception as e:
        logger.debug("Mermaid temp startup cleanup failed: %s", e, exc_info=True)


def _cleanup_mermaid_temp_shutdown() -> None:
    """Clean mermaid viewer temp files only if viewer was used this session."""
    if not config_module.get("render_diagram_viewer_enabled", False):
        return
    try:
        from .tools.mermaid_viewer import cleanup_temp_dir, was_viewer_used

        if not was_viewer_used():
            return
        cleanup_temp_dir()
    except Exception as e:
        logger.debug("Mermaid temp shutdown cleanup failed: %s", e, exc_info=True)


# Startup: clean stale files from previous sessions.
_cleanup_mermaid_temp_startup()
# Shutdown: clean only if viewer was actually used this session.
atexit.register(_cleanup_mermaid_temp_shutdown)


def _parse_watcher_flag(value: Optional[str]) -> bool:
    """Parse the --watcher flag value.

    None = not provided (disabled).
    'true'/'1'/'yes' = enabled (const from nargs='?').
    'false'/'0'/'no' = explicitly disabled.
    """
    if value is None:
        return False
    return value.lower() not in ("0", "no", "false")


def _get_watcher_enabled(args) -> bool:
    """Determine if the watcher should be enabled for the serve subcommand.

    Precedence (highest to lowest):
      1. --watcher CLI flag
      2. config file "watch" key  (JCODEMUNCH_WATCH env var is a fallback for this key
         when it is absent from config.jsonc — handled by config._apply_env_var_fallback)
    """
    flag = getattr(args, "watcher", None)
    if flag is not None:
        return _parse_watcher_flag(flag)
    return config_module.get("watch", False)


_BOOL_TRUE = frozenset(("true", "1", "yes", "on"))
_BOOL_FALSE = frozenset(("false", "0", "no", "off"))


def _coerce_arguments(arguments: dict, schema: dict) -> dict:
    """Coerce stringified values to their expected types per JSON schema.

    Handles boolean ("true"/"false"), integer ("5"), and number ("3.14")
    without eval. Unknown or already-correct types are passed through unchanged.
    """
    props = schema.get("properties", {})
    if not props:
        return arguments
    result = {}
    for k, v in arguments.items():
        if k in props and isinstance(v, str):
            expected = props[k].get("type")
            if expected == "boolean":
                if v.lower() in _BOOL_TRUE:
                    v = True
                elif v.lower() in _BOOL_FALSE:
                    v = False
            elif expected == "integer":
                try:
                    v = int(v)
                except (ValueError, TypeError):
                    pass
            elif expected == "number":
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    pass
        result[k] = v
    return result


_TOOL_SCHEMAS: dict[str, dict] | None = None


def _build_language_enum() -> list[str]:
    """Build language enum from config, falling back to all registry languages."""
    languages = config_module.get("languages")
    if languages is None:
        from .parser.languages import LANGUAGE_REGISTRY

        return sorted(LANGUAGE_REGISTRY.keys())
    return languages


async def _ensure_tool_schemas() -> dict[str, dict]:
    """Lazy-initialize the tool name → inputSchema lookup for type coercion.

    Uses our own list_tools() — no coupling to private MCP SDK internals.
    Populated once on the first tool call, then cached for the process lifetime.
    Also includes backward-compat aliases so coercion/validation works when
    call_tool receives old tool names.
    """
    global _TOOL_SCHEMAS
    if _TOOL_SCHEMAS is None:
        tools = await list_tools()
        _TOOL_SCHEMAS = {t.name: t.inputSchema for t in tools if t.inputSchema}
        for tool in _raw_catalog_tools():
            if tool.inputSchema:
                _TOOL_SCHEMAS.setdefault(tool.name, tool.inputSchema)
        # Backward-compat aliases are no longer listed, but callers may still
        # dispatch them directly. Validate/coerce them with their original
        # schemas so legacy arg names like symbol_id/file_paths keep working.
        for tool in _legacy_reading_tool_schemas():
            if tool.inputSchema:
                _TOOL_SCHEMAS.setdefault(tool.name, tool.inputSchema)
    return _TOOL_SCHEMAS


# Create server
server = Server("jcodemunch-mcp")


# Handshake watchdog: a stderr diagnostic that fires when the client never
# completes an MCP handshake / never calls a handler. Reproduces the
# Codex-CLI hang described in the v1.81.3 client report — under that bug,
# `uvx` chatter on stdout corrupted the first frame and the client sat
# silent for 5h+. This event is set on the first call into any MCP
# handler (list_tools / list_resources / list_prompts / get_prompt /
# call_tool); the watchdog in run_stdio_server prints a one-line hint to
# stderr if it stays unset past JCODEMUNCH_HANDSHAKE_TIMEOUT (default 5s).
_handshake_event: Optional[asyncio.Event] = None


def _signal_handshake() -> None:
    """Mark the handshake watchdog as satisfied. Idempotent and cheap."""
    ev = _handshake_event
    if ev is not None and not ev.is_set():
        ev.set()


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    _signal_handshake()
    return _build_tools_list()


def _build_tools_list() -> list[Tool]:
    """Build the full tool list, applying config-driven filtering and overrides."""
    all_tools = [
        Tool(
            name="summarize_repo",
            description=(
                "Re-run AI summarization on all symbols in an existing index. "
                "Use this when index_folder completed but AI summaries are missing — "
                "e.g., the background summarization thread was interrupted, AI was disabled "
                "at index time, or the summarizer provider wasn't configured yet. "
                "With force=true (recommended), clears all existing summaries and re-runs "
                "the full 3-tier pipeline (docstring → AI → signature fallback)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or local/hash)",
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "If true, clear all existing summaries and re-summarize every symbol. "
                            "Required when index_folder already applied signature fallbacks. "
                            "If false, only process symbols with no summary at all."
                        ),
                        "default": False,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="index_file",
            description="Index a single file within an existing index. Surgical update after edits. The file must be under an already-indexed folder's source_root. Can also add new files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to index.",
                    },
                    "use_ai_summaries": {
                        "type": "boolean",
                        "description": "Generate symbol summaries via AI. When false, falls back to docstrings or signature.",
                        "default": True,
                    },
                    "context_providers": {
                        "type": "boolean",
                        "description": "Whether to run context providers",
                        "default": True,
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="import_runtime_signal",
            description=(
                "Ingest a runtime trace file into the runtime_* tables for the target "
                "repo. source='otel' takes OTel JSON / JSON-Lines / .gz and maps spans "
                "via (file_path, line_no, function_name); source='sql_log' takes "
                "pg_stat_statements CSV or a generic SQL JSON-Lines log and maps queries "
                "via referenced tables (file-stem match) and dbt/SQLMesh column metadata; "
                "source='stack_log' takes a plain-text application log or JSON-Lines "
                "record set with Python / JVM / Node.js tracebacks and writes to both "
                "runtime_calls (severity-agnostic rollup) and runtime_stack_events "
                "(per-severity counts: error/warn/info). Returns {records, mapped, "
                "unmapped, redactions_fired, unmapped_reasons, evicted} plus source-"
                "specific fields (columns_recorded for sql_log; severity_counts and "
                "frames for stack_log). PII is redacted at the chokepoint by default. "
                "apm is reserved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["otel", "sql_log", "stack_log", "apm"],
                        "description": "Trace source format. Phases 1+4+5 accept 'otel', 'sql_log', and 'stack_log'.",
                        "default": "otel",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute filesystem path to the trace file",
                    },
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/name) — defaults to the current directory's resolved repo",
                    },
                    "redact_enabled": {
                        "type": "boolean",
                        "description": "Override the runtime_redact_enabled config key. Disable ONLY for offline debugging on synthetic data.",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="find_hot_paths",
            description=(
                "Top-N symbols ranked by total runtime hit count across ingested traces, "
                "with per-symbol p50/p95 latency, sources contributing, and last_seen. "
                "Optionally filtered by a name substring. Pairs with get_blast_radius to "
                "answer 'is this PR touching code that runs 4M times/day?' Returns an "
                "empty results list when no traces have been ingested."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/name)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive substring filter on symbol name",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Cap on returned rows (default 20, max 200)",
                        "default": 20,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="list_repos",
            description=(
                "List all indexed repositories. "
                "START HERE before using Grep/Read/search tools — check if the project is "
                "already indexed, then use search_symbols / get_symbol_source instead of "
                "native file reads. If jcodemunch tools appear as deferred in your tool list, "
                "call ToolSearch to load their schemas first."
                if config_module.get("discovery_hint", True)
                else "List all indexed repositories."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="resolve_repo",
            description="Resolve a filesystem path to its indexed repo identifier. O(1) lookup — faster than list_repos for finding a single repo. Accepts repo root, worktree, subdirectory, or file path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute filesystem path (repo root, worktree, subdirectory, or file)",
                    }
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="search_text",
            description="Full-text search across indexed file contents. Useful when symbol search misses (e.g., string literals, comments, config values). Supports regex (is_regex=true) and context lines around matches (context_lines=N, like grep -C).",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Text to search for. Case-insensitive substring by default. Set is_regex=true for full regex (e.g. 'estimateToken|tokenEstimat|\\.length.*0\\.25'). Limits: 500 chars plain, 200 chars when is_regex=true. Split longer alternations into multiple calls.",
                    },
                    "is_regex": {
                        "type": "boolean",
                        "description": "When true, treat query as a Python regex (re.search, case-insensitive). Supports alternation (|), character classes, lookaheads, etc. Max 200 chars; nested quantifiers (e.g. '(a+)+') are rejected to prevent catastrophic backtracking.",
                        "default": False,
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to filter files (e.g., '*.py')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching lines to return",
                        "default": 20,
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of context to include before and after each match (like grep -C N). Essential for understanding code around matches.",
                        "default": 0,
                    },
                },
                "required": ["repo", "query"],
            },
        ),
        Tool(
            name="find_references",
            description="Find all files that import or reference an identifier via the import graph. Answers 'where is this imported / re-exported?'. SCOPE: import sites + dbt `{{ ref() }}` edges + (when `include_call_chain=true`) symbols whose bodies textually mention the identifier. Does NOT exhaustively enumerate every call site across the codebase — for that, combine with search_text or use get_call_hierarchy on the resolved symbol_id. Use `identifiers` for batch queries. With `quick=true`, returns a lightweight is_referenced {bool} envelope (import_count + content_count) for fast dead-code detection — the former check_references mode.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository identifier"},
                    "mode": {
                        "type": "string",
                        "description": "Operation mode: 'refs' (default, original find_references), 'importers' (former find_importers — find files importing a given file), 'related' (former get_related_symbols — find symbols related to a given symbol).",
                        "enum": ["refs", "importers", "related"],
                        "default": "refs",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "For mode='importers': target file path within the repo. Use for single-file queries.",
                    },
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "For mode='importers': list of target file paths for batch queries. Cannot be used together with file_path.",
                    },
                    "cross_repo": {
                        "type": "boolean",
                        "default": False,
                        "description": "For mode='importers': when true, also search other indexed repos for cross-repo importers.",
                    },
                    "symbol_id": {
                        "type": "string",
                        "description": "For mode='related': ID of the symbol to find relatives for.",
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Symbol or module name to search for (e.g. 'bulkImport', 'IntakeService'). Use for single-identifier queries. Cannot be used together with identifiers.",
                    },
                    "identifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of symbol or module names to search for (batch mode). Returns a results array. Cannot be used together with identifier.",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum results",
                    },
                    "include_call_chain": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true (singular mode only), each reference entry includes calling_symbols: symbols in that file whose bodies mention the identifier. Default false.",
                    },
                    "quick": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true, return a lightweight is_referenced {bool} envelope with import_count and content_count instead of the full reference list. This is the merged check_references mode for quick dead-code detection.",
                    },
                    "search_content": {
                        "type": "boolean",
                        "default": True,
                        "description": "Also search file contents, not just imports (quick mode only). Default true.",
                    },
                    "max_content_results": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max files to return per identifier for content search (quick mode only).",
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="search_columns",
            description="Search column metadata across indexed models. Works with any ecosystem provider that emits column data (dbt, SQLMesh, database catalogs, etc.). Returns model name, file path, column name, and description. Use instead of grep/search_text for column discovery — 77% fewer tokens.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (matches column names and descriptions)",
                    },
                    "model_pattern": {
                        "type": "string",
                        "description": "Optional glob to filter by model name (e.g., 'fact_*', 'dim_provider')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 20,
                    },
                },
                "required": ["repo", "query"],
            },
        ),
        Tool(
            name="register_edit",
            description="Register file edits to invalidate caches. Call after editing files to clear BM25 cache and search result cache for the repo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier.",
                    },
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths that were edited.",
                    },
                    "reindex": {
                        "type": "boolean",
                        "description": "If True, also reindex the files.",
                        "default": False,
                    },
                },
                "required": ["repo", "file_paths"],
            },
        ),
        Tool(
            name="get_dependency_graph",
            description="Get the file-level dependency graph for a given file. Traverses import relationships up to 3 hops. Use to understand what a file depends on ('imports'), what depends on it ('importers'), or both. Prerequisite for blast radius analysis. Set cross_repo=true to include cross-repository edges.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "file": {
                        "type": "string",
                        "description": "File path within the repo (e.g. 'src/server.py')",
                    },
                    "direction": {
                        "type": "string",
                        "description": "'imports' (files this file depends on), 'importers' (files that depend on this file), or 'both'",
                        "enum": ["imports", "importers", "both"],
                        "default": "imports",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Number of hops to traverse (1–3)",
                        "default": 1,
                    },
                    "cross_repo": {
                        "type": "boolean",
                        "description": "When true, include cross-repo edges (imports that resolve to packages in other indexed repos). Default: false.",
                        "default": False,
                    },
                },
                "required": ["repo", "file"],
            },
        ),
        Tool(
            name="get_class_hierarchy",
            description="Get the full inheritance hierarchy for a class: ancestors (base classes via extends/implements) and descendants (subclasses/implementors). Works across Python, Java, TypeScript, C#, and any language where class signatures contain 'extends' or 'implements'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "class_name": {
                        "type": "string",
                        "description": "Name of the class to analyse",
                    },
                },
                "required": ["repo", "class_name"],
            },
        ),
        Tool(
            name="get_blast_radius",
            description="Find all files affected by changing a symbol. Returns confirmed files (import + name match) and potential files (import only, e.g. wildcard). Use before renaming or deleting a symbol. Set cross_repo=true to also find consumers in other indexed repos. Set include_source=true to get source snippets at each reference site (fix-ready context in one call). For automated edit plans, use plan_refactoring instead.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name or ID to analyse (e.g. 'calculateScore' or a full symbol ID)",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Import hops to traverse (1 = direct importers only, max 3). Default 1.",
                        "default": 1,
                    },
                    "include_depth_scores": {
                        "type": "boolean",
                        "description": "When true, adds impact_by_depth (files grouped by hop distance) and per-depth risk scores. overall_risk_score and direct_dependents_count are always included. Default false.",
                        "default": False,
                    },
                    "cross_repo": {
                        "type": "boolean",
                        "description": "When true, also find files in other indexed repos that consume this repo's package. Default: false.",
                        "default": False,
                    },
                    "call_depth": {
                        "type": "integer",
                        "description": "When > 0, also find symbols that *call* this symbol (call-level analysis). Returns a callers list alongside the import-level confirmed/potential. Max 3. Default 0 (disabled).",
                        "default": 0,
                    },
                    "fqn": {
                        "type": "string",
                        "description": "PHP fully-qualified class name (e.g. 'App\\Models\\User'). Resolves to symbol via PSR-4. Alternative to symbol.",
                    },
                    "decorator_filter": {
                        "type": "string",
                        "description": "Optional: filter confirmed results to only those containing symbols with this decorator (case-insensitive substring match)",
                    },
                    "include_source": {
                        "type": "boolean",
                        "description": "When true, each confirmed file includes source_snippets (lines referencing the symbol) and symbols_in_file (nearby symbol signatures). Use for fix-ready context without extra tool calls. Default false.",
                        "default": False,
                    },
                    "source_budget": {
                        "type": "integer",
                        "description": "Max tokens for source snippets across all files (default 8000). Files are prioritized by reference count.",
                        "default": 8000,
                    },
                    "include_decisions": {
                        "type": "boolean",
                        "description": "When true, attach a read-only 'decisions' block: decision-bearing commits (revert/perf/refactor/rename/bugfix) mined from the git history of the focal symbol's file and the confirmed affected files, plus a volatility read ('3 reverts + 2 perf rewrites in 180d — review before changing'). Surfaced from the commit record; nothing is persisted. Default false (spends a few git-log calls).",
                        "default": False,
                    },
                },
                "required": ["repo", "symbol"],
            },
        ),
        Tool(
            name="get_call_hierarchy",
            description=(
                "Return incoming callers and outgoing callees for a symbol, N levels deep. "
                "Uses AST-derived call detection: callers = symbols in importing files that "
                "mention this name; callees = imported symbols mentioned in this symbol's body. "
                "Useful for understanding how a symbol fits into the call graph before refactoring. "
                "Set include_impact=true for a transitive 'what breaks if I delete this?' analysis "
                "with affected symbols grouped by file and call-chain paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "symbol_id": {
                        "type": "string",
                        "description": "Symbol name or full ID to analyse. Use search_symbols to find IDs.",
                    },
                    "chains": {
                        "type": "boolean",
                        "description": "When true, also discover signal chains (HTTP routes, CLI commands, etc.) that involve this symbol. Merges get_signal_chains functionality.",
                        "default": False,
                    },
                    "kind": {
                        "type": "string",
                        "description": "When chains=true: filter gateways by kind (http, cli, event, task, main, test).",
                        "enum": ["http", "cli", "event", "task", "main", "test"],
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "When chains=true: BFS depth limit per chain (1-8, default 5). Also limits signal chain discovery depth.",
                        "default": 5,
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["callers", "callees", "both"],
                        "description": "'callers' = who calls this symbol; 'callees' = what this symbol calls; 'both' (default).",
                        "default": "both",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum hops to traverse (1–5). Default 3.",
                        "default": 3,
                    },
                    "include_impact": {
                        "type": "boolean",
                        "description": "When true, additionally walk the call graph transitively and return an 'impact' key with affected symbols grouped by file and call-chain paths (the former get_impact_preview behavior). Use this before deleting or renaming a symbol to understand full impact. Default false.",
                        "default": False,
                    },
                    "include_decisions": {
                        "type": "boolean",
                        "description": "When true and include_impact is true, attach a read-only 'decisions' block inside the impact result: decision-bearing commits (revert/perf/refactor/rename/bugfix) mined from the git history of the focal symbol's file and the impacted files, plus a volatility read. Surfaced from the commit record; nothing is persisted. Default false (spends a few git-log calls).",
                        "default": False,
                    },
                },
                "required": ["repo", "symbol_id"],
            },
        ),
        Tool(
            name="get_symbol_provenance",
            description=(
                "Trace the complete authorship lineage and evolution narrative of a symbol "
                "through git history. Returns every commit that touched the symbol (or its file), "
                "classified into semantic categories (creation, bugfix, refactor, feature, perf, "
                "rename, revert, etc.) with extracted commit intent. Includes a human-readable "
                "narrative summarising who created it, why, how it evolved, and how volatile it is. "
                "Use before refactoring unfamiliar code to understand the 'why' behind it. "
                "Requires a locally indexed repo (index_folder)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name or full ID as returned by search_symbols.",
                    },
                    "max_commits": {
                        "type": "integer",
                        "description": "Maximum commits to analyse (default 25, max 100).",
                        "default": 25,
                    },
                },
                "required": ["repo", "symbol"],
            },
        ),
        Tool(
            name="get_pr_risk_profile",
            description=(
                "Produce a unified risk assessment for all changes between two git refs (branch, PR, "
                "or SHA range). Fuses five signals — blast radius, complexity, churn, test gaps, "
                "and change volume — into a single composite risk_score (0.0–1.0) with actionable "
                "recommendations. Returns the top-5 riskiest changed symbols, untested symbols, "
                "and per-signal breakdowns. Designed for CI gating and code review workflows. "
                "Requires a locally indexed repo (index_folder)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "base_ref": {
                        "type": "string",
                        "description": "Base SHA/ref to compare from. Defaults to the SHA stored at index time.",
                    },
                    "head_ref": {
                        "type": "string",
                        "description": "Head SHA/ref to compare to (default 'HEAD').",
                        "default": "HEAD",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Churn look-back window in days (default 90).",
                        "default": 90,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="check_safe",
            description=(
                "Composite preflight: can this symbol be safely deleted or edited? "
                "Combines import analysis (cross-repo), reference check (quick=true), dead-code "
                "confidence, runtime evidence, and entry-point heuristics into a "
                "single verdict + one-line recommended_action. Use mode='delete' "
                "for deletion safety (verdict: safe_to_delete / test_coverage_only / "
                "internal_only / internal_uses_blocking / external_uses_blocking / "
                "cross_repo_blocking / runtime_observed / entry_point) or mode='edit' "
                "for edit safety (verdict: safe_to_edit / untested / complexity_risk / "
                "signature_impact / runtime_critical). Top-5 blockers ranked by "
                "severity. Read-only — never mutates the codebase."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository identifier"},
                    "symbol": {
                        "type": "string",
                        "description": "Symbol ID or name to evaluate for safety.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["delete", "edit"],
                        "default": "delete",
                        "description": "'delete' checks if the symbol can be removed; 'edit' checks regression risk if modified.",
                    },
                    "cross_repo": {
                        "type": "boolean",
                        "description": "Include other indexed repos in the analysis (default true).",
                        "default": True,
                    },
                    "include_runtime": {
                        "type": "boolean",
                        "description": "Consult runtime_calls for production evidence (default true).",
                        "default": True,
                    },
                },
                "required": ["repo", "symbol"],
            },
        ),
        Tool(
            name="find_implementations",
            description=(
                "Find concrete implementations of an interface, abstract class, or method. "
                "Multi-source resolution with confidence scoring: LSP dispatch (1.0), AST class "
                "hierarchy (0.85), duck-typed name match (0.65), decorator handler (0.45). "
                "Classifies each impl (subclass_override / interface_impl / duck_typed / "
                "decorator_handler / subclass), ranks by PageRank × byte_length, attaches "
                "differs_by breakdown. Optional cross_repo=true surfaces impls in other indexed "
                "repos via the package registry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository identifier"},
                    "symbol": {
                        "type": "string",
                        "description": "Symbol ID or name of the interface/abstract/method to analyse.",
                    },
                    "relationship_kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional whitelist: subclass_override, interface_impl, duck_typed, "
                            "decorator_handler, subclass. Defaults to all."
                        ),
                    },
                    "include_subclasses": {
                        "type": "boolean",
                        "description": "Walk class hierarchy for class-kind targets (default true).",
                        "default": True,
                    },
                    "cross_repo": {
                        "type": "boolean",
                        "description": "Also search other indexed repos via the package registry (default false).",
                        "default": False,
                    },
                    "rank_by_importance": {
                        "type": "boolean",
                        "description": "Sort by confidence then PageRank × byte_length (default true).",
                        "default": True,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on returned implementations (default 50).",
                        "default": 50,
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "Hard cap on response payload (default 4000).",
                        "default": 4000,
                    },
                },
                "required": ["repo", "symbol"],
            },
        ),
        Tool(
            name="plan_refactoring",
            description=(
                "Generate edit-ready refactoring instructions for renaming, moving, extracting, or "
                "changing the signature of a symbol. Returns {old_text, new_text} blocks for every "
                "affected file — directly compatible with Edit tool. Handles import rewrites, "
                "collision detection, new file generation, and multi-file coordination. "
                "Use BEFORE executing any multi-file refactoring to get a complete edit plan in one call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "symbol": {
                        "type": "string",
                        "description": (
                            "Symbol name or ID to refactor. For extract, comma-separated list "
                            "(e.g. 'helper,process_data')."
                        ),
                    },
                    "refactor_type": {
                        "type": "string",
                        "enum": ["rename", "move", "extract", "signature"],
                        "description": "Type of refactoring to plan.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New name for rename operations.",
                    },
                    "new_file": {
                        "type": "string",
                        "description": "Destination file path for move/extract operations.",
                    },
                    "new_signature": {
                        "type": "string",
                        "description": "New function signature (e.g. 'foo(x, y, z=0)').",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Import hops to traverse (1-3, default 2).",
                        "default": 2,
                    },
                },
                "required": ["repo", "symbol", "refactor_type"],
            },
        ),
        Tool(
            name="get_dead_code_v2",
            description=(
                "Find likely-dead functions and methods using three independent evidence signals: "
                "(1) the symbol's file is not reachable from any entry point via the import graph "
                "(filename heuristic + package.json main/module/exports/bin), "
                "(2) no indexed symbol calls this symbol in the call graph, "
                "(3) the symbol name is not re-exported from any __init__ or barrel file "
                "(recursively follows CJS `module.exports = require(...)` and ES `export * from`). "
                "Each result includes a confidence score (0.33 = 1 signal, 0.67 = 2 signals, 1.0 = all 3). "
                "More reliable than single-signal dead-code detection. "
                "Use min_confidence=0.67 for high-confidence results only. "
                "v1.80.7+ — `max_results` (default 100) caps response size; "
                "`file_pattern` scopes analysis to a glob like `src/**`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "min_confidence": {
                        "type": "number",
                        "description": "Minimum confidence threshold 0.0–1.0 (default 0.5 = at least 2/3 signals).",
                        "default": 0.5,
                    },
                    "include_tests": {
                        "type": "boolean",
                        "description": "Include test files in analysis (default false).",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on returned dead symbols (default 100, 0 = unlimited). _meta.truncated + _meta.total_matches flag when capped.",
                        "default": 100,
                        "minimum": 0,
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional glob (e.g. `src/**`, `*.py`) — only analyse symbols whose file matches.",
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_symbol_complexity",
            description=(
                "Return cyclomatic complexity, nesting depth, and parameter count for a single symbol. "
                "Complexity data is stored at index time (requires jcodemunch-mcp >= 1.16 / INDEX_VERSION 7). "
                "assessment field: 'low' (1-4), 'medium' (5-10), 'high' (11+). "
                "Re-index the repo if all metrics show 0 (pre-1.16 index)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "symbol_id": {
                        "type": "string",
                        "description": "Full symbol ID as returned by search_symbols or get_file_outline.",
                    },
                },
                "required": ["repo", "symbol_id"],
            },
        ),
        Tool(
            name="get_repo_health",
            description=(
                "Return a one-call triage snapshot of the entire repository: symbol counts, "
                "dead code %, average cyclomatic complexity, top 5 hotspots, dependency cycle count, "
                "and unstable module count. "
                "Designed to be the first tool called in any new session — one call gives a complete "
                "picture to guide follow-up analysis."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Churn look-back window for hotspot calculation (default 90).",
                        "default": 90,
                    },
                    "detailed": {
                        "type": "boolean",
                        "description": "When true, include additional detail sub-sections (cycles, coupling, layer violations, extraction candidates, hotspots, untested symbols, churn rates). Default false.",
                        "default": False,
                    },
                    "file_path": {
                        "type": "string",
                        "description": "When detailed=true, scope extraction candidates and coupling to this file (e.g. 'src/utils.py').",
                    },
                    "rules": {
                        "type": "array",
                        "description": "When detailed=true, layer definitions for violation checking. Each entry: {name, paths: [...], may_not_import: [...]}. If omitted, reads from .jcodemunch.jsonc architecture.layers.",
                        "items": {"type": "object"},
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "When detailed=true, number of hotspot results to return (default 20).",
                        "default": 20,
                    },
                    "min_confidence": {
                        "type": "number",
                        "description": "When detailed=true, minimum confidence for untested symbols (0.0-1.0, default 0.5).",
                        "default": 0.5,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "When detailed=true, cap on untested symbols returned (default 100).",
                        "default": 100,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="search_ast",
            description=(
                "Cross-language AST pattern matching. Finds structural code patterns "
                "across all 70+ indexed languages using a single query — no need to know "
                "language-specific AST node types. Two modes: (1) preset anti-patterns "
                "(empty_catch, bare_except, deeply_nested, nested_loops, god_function, "
                "eval_exec, hardcoded_secret, todo_fixme, magic_number, reassigned_param), "
                "or (2) custom mini-DSL (call:*.unwrap, string:/password/i, comment:/TODO/i, "
                "nesting:5+, loops:3+, lines:80+). Use category='all' to run every preset "
                "at once, or category='security'/'error_handling'/'complexity'/'performance'/"
                "'maintenance' for a focused scan. Every match is attributed to its enclosing "
                "indexed symbol with complexity metadata. Requires a locally indexed repo."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Preset name (empty_catch, bare_except, deeply_nested, nested_loops, "
                            "god_function, eval_exec, hardcoded_secret, todo_fixme, magic_number, "
                            "reassigned_param) or custom query (call:NAME, string:/REGEX/i, "
                            "comment:/REGEX/i, nesting:N+, loops:N+, lines:N+). "
                            "Mutually exclusive with category."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Run all presets in a category: security, error_handling, "
                            "complexity, performance, maintenance, or all."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "description": "Restrict scan to one language (e.g. 'python', 'typescript').",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob filter on file paths (e.g. 'src/**/*.py').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on total matches returned (default 50).",
                        "default": 50,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="find_similar_symbols",
            description=(
                "Find clusters of similar functions/methods/classes — consolidation candidates. "
                "Blends three signals: semantic (embedding cosine when embed_repo has run), "
                "structural (signature-token Jaccard + size ratio), and behavioral (callee-set Jaccard). "
                "Runs union-find clustering, classifies each cluster (near_duplicate / similar_logic / "
                "parallel_implementation), picks a canonical symbol per cluster (highest PageRank), "
                "and surfaces 'differs_by' breakdowns so an agent can recommend keep-this/replace-those. "
                "Pre-filters via BM25 inverted index — sub-N^2 on large repos. Degrades gracefully "
                "without embeddings (mode='structural'). Skip tests/dunders/generated files by default."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum combined similarity to form a cluster edge (0.0–1.0). Default 0.80.",
                        "default": 0.80,
                    },
                    "min_size": {
                        "type": "integer",
                        "description": "Minimum byte_length per symbol (default 30; filters out getters/wrappers).",
                        "default": 30,
                    },
                    "max_clusters": {
                        "type": "integer",
                        "description": "Cap on clusters returned (default 25).",
                        "default": 25,
                    },
                    "include_tests": {
                        "type": "boolean",
                        "description": "When False (default), test files are skipped — tests intentionally share shapes.",
                        "default": False,
                    },
                    "scope": {
                        "type": "string",
                        "description": "Optional glob to limit to a subdirectory (e.g. 'src/core/*').",
                    },
                    "include_kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Symbol kind whitelist. Defaults to ['function', 'method', 'class'].",
                    },
                    "semantic_weight": {
                        "type": "number",
                        "description": "Embedding weight when embeddings are present (0.0–1.0). Default 0.6.",
                        "default": 0.6,
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "Hard cap on the response's payload (default 4000).",
                        "default": 4000,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_repo_map",
            description=(
                "Query-less, token-budgeted, signature-level overview of a repository. "
                "Groups symbols by file, ranks files by PageRank on the import graph, and "
                "greedy-packs signatures (not bodies) under token_budget. Designed for "
                "cold-start orientation — 'I just cloned this repo, what matters here?'. "
                "Pair with get_tectonic_map (module topology) "
                "and get_ranked_context (query-driven) once you know what to ask for. "
                "When group_by='flat', returns a flat ranked list of most architecturally "
                "important symbols by PageRank or in-degree centrality — the same as the "
                "former get_symbol_importance tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["map", "outline"],
                        "default": "map",
                        "description": "'map' (default) returns signature-level map grouped by file or flat; 'outline' returns a lighter directory/language/symbol count overview (former get_repo_outline).",
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "Hard cap on returned tokens (default 2048). Ignored when group_by='flat'.",
                        "default": 2048,
                    },
                    "scope": {
                        "type": "string",
                        "description": "Optional glob to limit to a subdirectory (e.g. 'src/core/*').",
                    },
                    "max_per_file": {
                        "type": "integer",
                        "description": "Max signatures emitted per file (default 5, capped at 50). Ignored when group_by='flat'.",
                        "default": 5,
                    },
                    "include_kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of symbol kinds to restrict results (e.g. ['class', 'function']).",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["file", "flat"],
                        "description": (
                            "'file' (default) groups symbols by file with greedy token packing. "
                            "'flat' returns a flat ranked list of most architecturally important "
                            "symbols by PageRank or in-degree centrality (the former "
                            "get_symbol_importance behaviour)."
                        ),
                        "default": "file",
                    },
                    "algorithm": {
                        "type": "string",
                        "enum": ["pagerank", "degree"],
                        "description": "'pagerank' (default) = full PageRank on import graph; 'degree' = simple in-degree count (faster). Only used when group_by='flat'.",
                        "default": "pagerank",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top symbols to return in flat mode (default 20, max 200). Only used when group_by='flat'.",
                        "default": 20,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="assemble_task_context",
            description=(
                "Task-aware single-call orchestrator. Auto-classifies task into "
                "explore/debug/refactor/extend/audit/review intent, runs the right sub-tools, "
                "returns one source-attributed capsule under token_budget."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository identifier"},
                    "task": {
                        "type": "string",
                        "description": "Natural-language task description. Anchors auto-extracted from task text.",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional anchor symbol IDs or names; auto-extracted from task when omitted.",
                    },
                    "intent": {
                        "type": "string",
                        "enum": [
                            "explore",
                            "debug",
                            "refactor",
                            "extend",
                            "audit",
                            "review",
                        ],
                        "description": "Optional override; auto-detected from task when omitted.",
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "End-to-end hard cap on returned tokens (default 8000).",
                        "default": 8000,
                    },
                    "include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of stages to run (e.g. ['anchor', 'blast', 'runtime']).",
                    },
                    "cross_repo": {
                        "type": "boolean",
                        "description": "When True, layer cross-repo signals (default false).",
                        "default": False,
                    },
                },
                "required": ["repo", "task"],
            },
        ),
        Tool(
            name="get_changed_symbols",
            description=(
                "Map a git diff to affected symbols: given two commits, returns which symbols "
                "were added, removed, modified, or renamed. Useful after merging a PR to answer "
                "'what actually changed?' for code review or regression triage. "
                "Requires a locally indexed repo (index_folder). "
                "Defaults to comparing current HEAD against the SHA stored at index time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier — must be locally indexed with index_folder",
                    },
                    "since_sha": {
                        "type": "string",
                        "description": "Compare from this git SHA or ref. Defaults to the SHA stored at index time.",
                    },
                    "until_sha": {
                        "type": "string",
                        "description": "Compare to this git SHA or ref (default 'HEAD').",
                        "default": "HEAD",
                    },
                    "include_blast_radius": {
                        "type": "boolean",
                        "description": "Also return downstream importers (blast radius) for each changed symbol (default false).",
                        "default": False,
                    },
                    "max_blast_depth": {
                        "type": "integer",
                        "description": "Hop limit when include_blast_radius=true (default 3, max 5).",
                        "default": 3,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="embed_repo",
            description=(
                "Precompute and cache symbol embeddings for semantic search. "
                "Optional warm-up: search_symbols with semantic=true lazily embeds missing "
                "symbols on first use, but embed_repo warms the cache upfront so the first "
                "semantic query returns immediately. "
                "Requires an embedding provider (JCODEMUNCH_EMBED_MODEL, "
                "GOOGLE_API_KEY+GOOGLE_EMBED_MODEL, or OPENAI_API_KEY+OPENAI_EMBED_MODEL)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "batch_size": {
                        "type": "integer",
                        "description": "Symbols per embedding batch (default 50).",
                        "default": 50,
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Recompute all embeddings even if they already exist (default false).",
                        "default": False,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_tectonic_map",
            description=(
                "Discover the logical module topology of a codebase by fusing three coupling signals: "
                "structural (import edges), behavioral (shared symbol references), and temporal "
                "(git co-churn). Returns tectonic plates (auto-detected file clusters), each with "
                "an anchor file, cohesion score, inter-plate coupling, and drifters (files whose "
                "directory doesn't match their logical module). Detects nexus plates (god-module risk: "
                "coupled to ≥4 other plates). No k parameter — plate count emerges from the topology. "
                "Use to find hidden module boundaries, misplaced files, and architectural drift."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or just repo name)",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Git co-churn look-back window in days (default 90)",
                        "default": 90,
                    },
                    "min_plate_size": {
                        "type": "integer",
                        "description": "Minimum files per plate to include; smaller groups go to isolated_files (default 2)",
                        "default": 2,
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="get_project_intel",
            description=(
                "Auto-discover and parse non-code knowledge files (Dockerfiles, CI configs, "
                "docker-compose, K8s manifests, .env templates, Makefiles, package.json scripts) "
                "and cross-reference them to indexed code symbols. Returns structured intelligence "
                "grouped by category: infra, ci, config, deps, api, data. "
                "For categories already in the index (OpenAPI, Terraform, GraphQL, Protobuf, dbt), "
                "pulls from the index directly. Requires a local index (index_folder)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or display name).",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category to return: all, infra, ci, config, deps, api, data.",
                        "default": "all",
                        "enum": ["all", "infra", "ci", "config", "deps", "api", "data"],
                    },
                    "scope_path": {
                        "type": "string",
                        "description": "Optional subpath (relative to source_root) to restrict intel discovery to a single workspace member — e.g. 'packages/api'. When omitted, the whole repo is scanned. Use `list_workspaces` to enumerate the available members. Cross-references still consult the global index so a package's container still resolves against repo-level code.",
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="list_workspaces",
            description=(
                "Enumerate monorepo workspace members for an indexed repo. Detects "
                "pnpm (pnpm-workspace.yaml), yarn/npm (package.json workspaces), "
                "turborepo (turbo.json), lerna (lerna.json), rush (rush.json), "
                "Go (go.work), and Cargo ([workspace] members). Returns "
                "[{path, package_name, manager}, ...] plus an `is_monorepo` flag "
                "and the list of managers that contributed. Use the returned "
                "`path` values as the `scope_path` argument on get_project_intel "
                "to retrieve per-package intel (Dockerfile / CI / deps) instead of "
                "the repo-wide aggregate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo or display name).",
                    },
                },
                "required": ["repo"],
            },
        ),
    ]
    # --- The Counter: register the front door ------
    all_tools = all_tools + _counter_front_door_tools()
    # Capture the raw catalog for menu/order before any surface-specific return.
    # This catalog intentionally excludes the front door and includes the
    # unified reading tools that replace the removed code-only definitions.
    raw_catalog = [t for t in all_tools if t.name not in _COUNTER_FRONT_DOOR]
    raw_catalog = raw_catalog + _reading_surface_tools()
    global _RAW_CATALOG
    _RAW_CATALOG = list(raw_catalog)
    surface = _effective_surface()
    if surface == "counter":
        # Collapse to the front door. Surface choice bypasses disabled_tools.
        keep = _COUNTER_FRONT_DOOR
        tools = [t for t in all_tools if t.name in keep]
        _apply_description_overrides(tools)
        return tools
    if surface in {"reading", "jmri"}:
        tools = _reading_surface_tools()
        _apply_description_overrides(tools)
        return tools
    # Non-counter surfaces: front-door tools stay hidden
    # (still callable via call_tool), only non-front-door tools appear.
    all_tools = list(raw_catalog)
    # Filter out disabled tools (simple set difference).
    disabled = config_module.get("disabled_tools", [])
    if disabled:
        disabled_set = set(disabled)
        tools = [t for t in all_tools if not _disabled_by_set(t.name, disabled_set)]
    else:
        tools = list(all_tools)

    # SQL gating: auto-disable search_columns when SQL not in languages
    languages = config_module.get("languages")
    if languages is not None and "sql" not in languages:
        tools = [t for t in tools if t.name != "search_columns"]

    # --- Compact schemas: strip rarely-used params ---------------------------
    if config_module.get("compact_schemas", False):
        for tool in tools:
            if not isinstance(tool.inputSchema, dict):
                continue
            props = tool.inputSchema.get("properties")
            if not props:
                continue
            strip_set = _COMPACT_STRIP_PARAMS.get(tool.name)
            if strip_set:
                for param in strip_set:
                    props.pop(param, None)
            # Demote large mechanical enums to free-string filters (capability
            # preserved; the tool accepts any string for these params).
            for param in _COMPACT_DEMOTE_ENUM_PARAMS:
                pschema = props.get(param)
                if isinstance(pschema, dict) and "enum" in pschema:
                    props[param] = {k: v for k, v in pschema.items() if k != "enum"}

    # Merge descriptions from config (runs after disabled_tools filter)
    _apply_description_overrides(tools)

    return tools


def _apply_description_overrides(tools: list) -> None:
    """Apply description overrides from config to tool schemas."""
    descriptions = config_module.get_descriptions()
    if not descriptions:
        return

    shared = descriptions.get("_shared", {})

    for tool in tools:
        raw = descriptions.get(tool.name)
        if raw is None:
            tool_desc: dict = {}
        elif isinstance(raw, str):
            # Flat format: "tool_name": "description" → override tool description only
            tool.description = raw
            tool_desc = {}
        else:
            tool_desc = raw

        # Nested format: override tool-level description via "_tool" key
        # "_tool": "" means "use hardcoded minimal base only" (empty string override)
        if "_tool" in tool_desc:
            tool.description = tool_desc["_tool"]

        # Override parameter descriptions (applies even if only _shared is set)
        if isinstance(tool.inputSchema, dict):
            props = tool.inputSchema.get("properties", {})
            for param_name, param_schema in props.items():
                if not isinstance(param_schema, dict):
                    continue
                # Tool-specific override takes precedence over _shared
                # Empty string means "use hardcoded minimal base only"
                desc_override = tool_desc.get(param_name)
                if desc_override is None:
                    desc_override = shared.get(param_name)
                if desc_override is not None:
                    props[param_name] = {**param_schema, "description": desc_override}


@server.list_resources()
async def list_resources() -> list[Resource]:
    """Return empty resource list for client compatibility (e.g. Windsurf)."""
    _signal_handshake()
    return []


_WORKFLOW_PROMPT_TEXT = """\
# jcodemunch-mcp — Workflow Guide

Use these tools instead of Grep/Read/search for any indexed repository.

## Step-by-step

1. **list_repos** — check if the project is already indexed.
   - If not found, run **index_folder** (local) or **index_repo** (GitHub URL).

2. **search_symbols** — find functions, classes, methods by name or description.
   - Use `detail_level: "full"` to get source inline, or follow up with **get_symbol_source**.

3. **get_context_bundle** — get symbol source + its imports in one call.

4. **search_text** — fall back to full-text / regex search for string literals or comments.

5. **get_file_outline** — list all symbols in a file without reading the whole thing.

## Claude Code deferred-tool note

jcodemunch tools may appear as *deferred* in your system-reminder. Call **ToolSearch** with
a query like `"list repos"` or `"search symbols"` to load the full schema before use.
Set `discovery_hint: false` in config.jsonc to suppress the reminder in tool descriptions.
"""

_EXPLORE_PROMPT_TEXT = """\
# Explore — Build a mental model of an unfamiliar repo

Goal: Onboard to a repo you've never seen before.

1. **list_repos** → check if indexed. If not, run **index_folder** (local) or **index_repo** (GitHub).
2. **get_repo_outline** → directory structure, languages, most-imported files, most-central symbols (PageRank).
3. **get_repo_health** → dead code %, avg complexity, hotspots, dependency cycles, unstable modules.
4. **get_file_outline** on the 2–3 most-central files → understand the core.
5. **get_class_hierarchy** → inheritance structure (if OOP codebase).
6. **get_dependency_graph** on the entry point file (`direction="importers"`, `depth=2`) → what depends on the core.
7. **search_symbols** with `sort_by="centrality"` → find the most important symbols across the repo.
"""

_ASSESS_PROMPT_TEXT = """\
# Assess — Pre-merge impact analysis

Goal: Understand the blast radius of a change before merging.

**Quick path** (one call): **get_pr_risk_profile** → unified risk score fusing blast radius, \
complexity, churn, test gaps, and change volume. Includes actionable recommendations.

**Deep path** (manual drill-down):
1. **get_changed_symbols** → map the git diff to added/removed/modified/renamed symbols.
2. **get_blast_radius** on each changed file → depth-scored transitive impact + `has_test_reach` per file.
3. **get_call_hierarchy** (include_impact=true) on key changed symbols → "what breaks?" analysis.
4. **get_symbol_provenance** on unfamiliar symbols → understand why the code exists before changing it.
5. **get_untested_symbols** on affected files → flag unreached symbols in the blast radius.
6. **get_coupling_metrics** on changed files → check if the change increases coupling.
7. **get_dependency_cycles** → check if the change introduces new cycles.
8. **search_ast** with `category='security'` on changed files → catch hardcoded secrets or eval() calls in the diff.
"""

_TRIAGE_PROMPT_TEXT = """\
# Triage — Diagnose a repo's code quality

Goal: Get a complete health picture in one guided session.

1. **get_repo_health** → one-call snapshot (dead code %, complexity, hotspots, cycles, unstable modules).
2. **get_dead_code_v2** with `min_confidence=0.8` → high-confidence dead code candidates for removal.
3. **get_untested_symbols** → functions with no test-file reachability.
4. **get_dependency_cycles** → full cycle list with file paths.
5. **get_hotspots** with `top_n=10`, `days=90` → highest-risk symbols by complexity × churn.
6. **get_layer_violations** → architectural boundary violations.
7. **get_extraction_candidates** → functions that should be refactored out.
8. **get_coupling_metrics** on hotspot files → instability analysis.
9. **search_ast** with `category='all'` → sweep for anti-patterns (empty catches, god functions, magic numbers, etc.).
"""

_TRACE_PROMPT_TEXT = """\
# Trace — Investigate a bug through the call graph

Goal: Follow a suspected bug from symptom to root cause.

1. **search_symbols** for the function name or error message keyword.
2. **get_symbol_source** on the suspect symbol → read the implementation.
3. **get_call_hierarchy** with `direction="callers"`, `depth=3` → who calls this?
4. **get_call_hierarchy** with `direction="callees"`, `depth=2` → what does it call?
5. **get_context_bundle** on the suspect symbol → full source + imports in one call.
6. **find_references** for the symbol name → all files that reference it.
7. **get_blast_radius** on the suspect file → what else could be affected?
"""


@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    """Return available workflow guidance prompts."""
    _signal_handshake()
    return [
        Prompt(
            name="workflow",
            description="Step-by-step guide for using jcodemunch-mcp tools in Claude Code.",
        ),
        Prompt(
            name="explore",
            description="Build a mental model of an unfamiliar repo.",
        ),
        Prompt(
            name="assess",
            description="Pre-merge impact analysis — blast radius, reachability, coupling.",
        ),
        Prompt(
            name="triage",
            description="Diagnose a repo's code quality — dead code, hotspots, cycles.",
        ),
        Prompt(
            name="trace",
            description="Investigate a bug through the call graph from symptom to root cause.",
        ),
    ]


_PROMPT_MAP: dict[str, tuple[str, str]] = {
    "workflow": (
        _WORKFLOW_PROMPT_TEXT,
        "jcodemunch-mcp workflow guide for Claude Code.",
    ),
    "explore": (
        _EXPLORE_PROMPT_TEXT,
        "Explore — build a mental model of an unfamiliar repo.",
    ),
    "assess": (_ASSESS_PROMPT_TEXT, "Assess — pre-merge impact analysis."),
    "triage": (_TRIAGE_PROMPT_TEXT, "Triage — diagnose a repo's code quality."),
    "trace": (_TRACE_PROMPT_TEXT, "Trace — investigate a bug through the call graph."),
}


@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    """Return the requested prompt content."""
    _signal_handshake()
    entry = _PROMPT_MAP.get(name)
    if entry is None:
        raise ValueError(f"Unknown prompt: {name}")
    text, description = entry
    return GetPromptResult(
        description=description,
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=text),
            )
        ],
    )


# Tools excluded from auto-watch (no folder target, meta-only, or file-path arg)
_AUTO_WATCH_EXCLUDED = frozenset(
    {
        "list_repos",
        "index_file",  # path arg is a file path, not a folder; requires repo already indexed
    }
)


def _get_source_root(repo: str, storage_path: Optional[str]) -> Optional[str]:
    """Resolve repo ID to folder path using IndexStore public API.

    Returns None if the repo is not indexed.
    """
    # Parse owner/name from repo ID (format: "owner/name" or "local/name-hash")
    parts = repo.split("/", 1)
    if len(parts) != 2:
        return None
    owner, name = parts

    try:
        from .storage import IndexStore

        store = IndexStore(base_path=storage_path)
        return store.get_source_root(owner, name)
    except Exception:
        logger.debug("Failed to resolve source_root for %s", repo, exc_info=True)
        return None


async def _auto_watch_if_needed(
    name: str, arguments: dict, storage_path: Optional[str]
) -> None:
    """Auto-watch hook: ensure unwatched repos are indexed before tool execution.

    Hook fires BEFORE tool dispatch to ensure the tool runs against fresh data.
    """
    global _watcher_manager

    # Check if watcher is running and auto-watch is enabled
    if _watcher_manager is None:
        return

    if not config_module.get("watch", False):
        return

    # Check if tool is excluded
    if name in _AUTO_WATCH_EXCLUDED:
        return

    # Extract folder from arguments
    folder: Optional[str] = None

    # Path-based tools
    if "path" in arguments:
        try:
            folder = str(Path(arguments["path"]).expanduser().resolve())
        except Exception:
            pass

    # Repo-based tools
    if not folder and "repo" in arguments:
        repo = arguments["repo"]
        if repo:
            folder = _get_source_root(repo, storage_path)

    if not folder:
        return

    # Check if already watched
    if _watcher_manager.is_watched(folder):
        return

    # Opportunistic standby takeover before indexing
    maybe_takeover = getattr(_watcher_manager, "maybe_takeover", None)
    if maybe_takeover is not None:
        result = await maybe_takeover(folder)
        if result.get("status") in {"started", "already_watched"}:
            await _watcher_manager.ensure_indexed(folder)
            return

    # Race-safe reindex, then start watching
    try:
        await _watcher_manager.ensure_indexed(folder)
        await _watcher_manager.add_folder(folder)
        logger.debug("Auto-watch: indexed and watching %s", folder)
    except Exception:
        logger.debug("Auto-watch failed for %s", folder, exc_info=True)


async def _handle_counter_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch the Counter front door (order / menu / route)."""
    if name == "order":
        return await _handle_order(arguments)
    if name == "menu":
        return _handle_menu(arguments)
    if name == "route":
        return await _handle_route(arguments)
    return [
        TextContent(
            type="text", text=json.dumps({"error": f"Unknown front-door tool '{name}'"})
        )
    ]


async def _handle_order(arguments: dict) -> list[TextContent] | CallToolResult:
    """order(action, args): validate against the catalog + charter gate, then
    re-enter the normal pipeline for the resolved action."""
    action = arguments.get("action")
    args = arguments.get("args") or {}
    if not isinstance(args, dict):
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "order 'args' must be an object."}, indent=2),
            )
        ]
    allow = bool(arguments.get("allow_state_change", False))
    err = _counter.order_gate(action, _catalog_names(), allow)
    if err is not None:
        return [
            TextContent(
                type="text", text=json.dumps({"error": err, "tool": "order"}, indent=2)
            )
        ]
    return await call_tool(action, dict(args))


def _handle_menu(arguments: dict) -> list[TextContent]:
    """menu(query?, limit?): search/browse the action catalog."""
    query = arguments.get("query")
    try:
        limit = int(arguments.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 200))
    rows = _counter.search_catalog(_catalog_rows(), query, limit)
    clean = [{k: v for k, v in r.items() if k != "_description"} for r in rows]
    payload = {
        "tool": "menu",
        "query": query or None,
        "count": len(clean),
        "total_actions": len(_catalog_names()),
        "actions": clean,
        "hint": "Dispatch with order(action, args). Get a task->action pick with route(task).",
    }
    return [TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))]


async def _handle_route(arguments: dict) -> list[TextContent] | CallToolResult:
    """route(task, repo?, execute?, model?): intent -> recommended action(s),
    optionally dispatching the top one in the same call."""
    task = arguments.get("task")
    if not task or not isinstance(task, str):
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": "route requires a 'task' string."}, indent=2),
            )
        ]
    repo = arguments.get("repo")
    execute = bool(arguments.get("execute", False))
    names = _catalog_names()
    recs = _counter.classify_intent(task, names)
    if not recs:  # fall back to catalog search when no curated rule matched
        for r in _counter.search_catalog(_catalog_rows(), task, 3):
            recs.append({"action": r["action"], "why": r["summary"]})
    for r in recs:
        tmpl = _counter.shape_execute_args(r["action"], repo, task)
        r["args_template"] = (
            tmpl
            if tmpl is not None
            else {"repo": repo or "<repo>", "_hint": "see menu for args"}
        )
        r["state_changing"] = _counter.is_state_changing(r["action"])
    payload = {"tool": "route", "task": task, "recommended": recs}
    if not recs:
        payload["hint"] = "No confident action match. Call menu(query=...) to browse."
        return [
            TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))
        ]
    if execute:
        action = recs[0]["action"]
        exec_args = _counter.shape_execute_args(action, repo, task)
        if exec_args is None:
            payload["executed"] = False
            payload["execute_error"] = (
                f"Cannot auto-build args for '{action}' from (repo, task). "
                f"Call order('{action}', args) with explicit arguments."
            )
            return [
                TextContent(
                    type="text", text=json.dumps(payload, separators=(",", ":"))
                )
            ]
        if _counter.is_state_changing(action):
            payload["executed"] = False
            payload["execute_error"] = (
                f"Top action '{action}' is state-changing; dispatch it explicitly via order(allow_state_change=true)."
            )
            return [
                TextContent(
                    type="text", text=json.dumps(payload, separators=(",", ":"))
                )
            ]
        result = await call_tool(action, exec_args)
        head = TextContent(
            type="text",
            text=json.dumps(
                {
                    "tool": "route",
                    "task": task,
                    "executed_action": action,
                    "args": exec_args,
                },
                separators=(",", ":"),
            ),
        )
        if isinstance(result, CallToolResult):
            # The routed action failed (isError); surface its content under the
            # route envelope and propagate the error signal rather than list()-ing
            # a non-iterable CallToolResult.
            return CallToolResult(
                content=[head, *result.content], isError=result.isError
            )
        return [head] + list(result)
    return [TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))]


def _error_call_result(text: str) -> CallToolResult:
    """Wrap an error payload so MCP clients that branch on ``isError`` see the
    failure (F-P01), while the JSON body stays in ``content`` for in-band
    parsers (the v1.108.30 contract). Success results stay a plain
    ``list[TextContent]`` (the SDK wraps them ``isError=False``), so this is
    additive on the wire — only failures gain the ``isError`` signal.
    """
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict) -> list[TextContent] | CallToolResult:
    """Handle tool calls."""
    _signal_handshake()
    storage_path = os.environ.get("CODE_INDEX_PATH")
    logger.info(
        "tool_call: %s args=%s",
        name,
        {k: v for k, v in arguments.items() if k != "content"},
    )

    _t0_call = time.perf_counter()
    _call_ok = True
    try:  # main handler try starts here, before coerce
        # Extract cross-cutting args that are not part of any tool's schema.
        # `format` controls compact-output encoding (see .encoding package).
        _requested_format = None
        if isinstance(arguments, dict) and "format" in arguments:
            _requested_format = arguments.pop("format")
        # Coerce stringified booleans/integers/numbers before routing
        schema = (await _ensure_tool_schemas()).get(name)
        if schema:
            arguments = _coerce_arguments(arguments, schema)
            try:
                jsonschema.validate(instance=arguments, schema=schema)
            except jsonschema.ValidationError as e:
                return _error_call_result(
                    json.dumps(
                        {"error": f"Input validation error: {e.message}"}, indent=2
                    )
                )

        # The Counter front door: order/menu/route. Handled before repo-scoped
        # strict-freshness/auto-watch (the front door isn't repo-scoped; order
        # re-enters call_tool for the real action, which then runs those hooks).
        if name in _COUNTER_FRONT_DOOR:
            return await _handle_counter_tool(name, arguments)

        # jcm#329: cheap per-tool argument validation BEFORE strict-freshness
        # waits and auto-watch reindexing. A call doomed to instant rejection
        # must not pay unbounded pre-dispatch work first (field report: 29s
        # to reject an over-long regex behind an auto-watch reindex).
        if name == "search_text":
            from .tools.search_text import validate_query_args

            _arg_err = validate_query_args(
                arguments.get("query", ""), bool(arguments.get("is_regex", False))
            )
            if _arg_err is not None:
                return _error_call_result(json.dumps(_arg_err, indent=2))

        # Strict freshness mode: wait for any in-progress reindex to complete
        # before serving query results (except for write/index tools).
        # MUST use asyncio.to_thread — threading.Event.wait() cannot run on the event loop.
        repo_arg = arguments.get("repo")
        if name not in _EXCLUDED_FROM_STRICT and repo_arg:
            strict_ms = config_module.get("strict_timeout_ms", 500)
            await asyncio.to_thread(
                await_freshness_if_strict, repo_arg, timeout_ms=strict_ms
            )

        # Project-level tool disabling: check if tool is disabled for this project
        # Global disabled tools are filtered out in list_tools() schema; project-level
        # rejection happens here since schema is global (can't be changed per-project).
        disabled_by = _disabled_tool_match(name, repo=repo_arg)
        if disabled_by is not None:
            canonical = _ALIAS_TO_CANONICAL.get(name, name)
            if disabled_by != name or canonical != name:
                alias_note = (
                    f" (matched disabled_tools entry '{disabled_by}' for "
                    f"canonical tool '{canonical}')"
                )
            else:
                alias_note = ""
            return _error_call_result(
                json.dumps(
                    {
                        "error": (
                            f"Tool '{name}' is disabled in this project's configuration{alias_note}. "
                            f"Project-level tool disabling is set via the 'disabled_tools' key "
                            f"in the .jcodemunch.jsonc file. Remove '{name}' from 'disabled_tools' to re-enable."
                        )
                    },
                    indent=2,
                )
            )

        # Auto-watch: ensure unwatched repos are indexed before tool execution
        try:
            await _auto_watch_if_needed(name, arguments, storage_path)
        except Exception:
            logger.debug("Auto-watch check failed", exc_info=True)

        # Progress notifications for long-running tools
        _progress_cb = None
        if name in (
            "index_content",
            "index_repo",
            "index_folder",
            "index_file",
            "embed_repo",
        ):
            try:
                from .progress import ProgressReporter, make_progress_notify

                _progress_notify = make_progress_notify(server)
                if _progress_notify:
                    _label = {
                        "index_repo": "Index",
                        "index_folder": "Index",
                        "index_content": "Index",
                        "index_file": "Index",
                        "embed_repo": "Embed",
                    }[name]
                    _reporter = ProgressReporter(_progress_notify, _label)
                    _progress_cb = _reporter.update
                    _reporter_ref = _reporter  # prevent GC
            except Exception:
                logger.debug("Progress setup failed", exc_info=True)

        if name == "index_content":
            from .tools.content_router import index_content

            result = await asyncio.to_thread(
                functools.partial(
                    index_content,
                    path=arguments.get("path"),
                    url=arguments.get("url"),
                    domain=arguments.get("domain", "both"),
                    use_ai_summaries=arguments.get(
                        "use_ai_summaries", _default_use_ai_summaries()
                    ),
                    use_embeddings=arguments.get("use_embeddings", "auto"),
                    incremental=arguments.get("incremental", True),
                    paths=arguments.get("paths"),
                    name=arguments.get("name"),
                    extra_ignore_patterns=arguments.get("extra_ignore_patterns"),
                    follow_symlinks=arguments.get("follow_symlinks", False),
                    identity_mode=arguments.get("identity_mode", "config"),
                    progress_cb=_progress_cb,
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            _result_cache_invalidate()
        elif name == "list_content":
            from .tools.content_router import list_content

            result = await asyncio.to_thread(
                functools.partial(
                    list_content,
                    repo=arguments["repo"],
                    domain=arguments.get("domain", "both"),
                    path_prefix=arguments.get("path_prefix", ""),
                    path_glob=arguments.get("path_glob"),
                    tree=arguments.get("tree", False),
                    include_summaries=arguments.get("include_summaries", False),
                    max_files=arguments.get("max_files"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
        elif name == "get_outline":
            from .tools.content_router import get_outline

            result = await asyncio.to_thread(
                functools.partial(
                    get_outline,
                    repo=arguments["repo"],
                    file_path=arguments.get("file_path", ""),
                    domain=arguments.get("domain", "auto"),
                    file_paths=arguments.get("file_paths"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
        elif name == "get_file":
            from .tools.content_router import get_file

            result = await asyncio.to_thread(
                functools.partial(
                    get_file,
                    repo=arguments["repo"],
                    file_path=arguments["file_path"],
                    domain=arguments.get("domain", "auto"),
                    start_line=arguments.get("start_line"),
                    end_line=arguments.get("end_line"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
        elif name == "search_units":
            from .tools.content_router import search_units

            result = await asyncio.to_thread(
                functools.partial(
                    search_units,
                    repo=arguments.get("repo"),
                    query=arguments["query"],
                    domain=arguments.get("domain", "both"),
                    file_path=arguments.get("file_path"),
                    path_glob=arguments.get("path_glob"),
                    max_results=arguments.get("max_results", 10),
                    mode=arguments.get("mode", "default"),
                    kind=arguments.get("kind"),
                    language=arguments.get("language"),
                    file_pattern=arguments.get("file_pattern"),
                    decorator=arguments.get("decorator"),
                    token_budget=arguments.get("token_budget"),
                    detail_level=arguments.get("detail_level", "standard"),
                    debug=arguments.get("debug", False),
                    fuzzy=arguments.get("fuzzy", False),
                    fuzzy_threshold=arguments.get("fuzzy_threshold", 0.4),
                    max_edit_distance=arguments.get("max_edit_distance", 2),
                    sort_by=arguments.get("sort_by", "relevance"),
                    semantic=arguments.get("semantic", False),
                    semantic_weight=arguments.get("semantic_weight", 0.5),
                    semantic_only=arguments.get("semantic_only", False),
                    fusion=arguments.get("fusion", False),
                    fqn=arguments.get("fqn"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
        elif name == "get_unit":
            from .tools.content_router import get_unit

            result = await asyncio.to_thread(
                functools.partial(
                    get_unit,
                    repo=arguments["repo"],
                    unit_id=arguments.get("unit_id"),
                    unit_ids=arguments.get("unit_ids"),
                    domain=arguments.get("domain", "auto"),
                    verify=arguments.get("verify", False),
                    verify_against=arguments.get("verify_against", "cache"),
                    context_lines=arguments.get("context_lines", 0),
                    fqn=arguments.get("fqn"),
                    source_start_line=arguments.get("source_start_line"),
                    source_end_line=arguments.get("source_end_line"),
                    max_source_lines=arguments.get("max_source_lines"),
                    max_source_bytes=arguments.get("max_source_bytes"),
                    max_total_source_bytes=arguments.get("max_total_source_bytes"),
                    strip_boilerplate=arguments.get("strip_boilerplate", False),
                    compress_code=arguments.get("compress_code", False),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
        elif name == "get_unit_context":
            from .tools.content_router import get_unit_context

            result = await asyncio.to_thread(
                functools.partial(
                    get_unit_context,
                    repo=arguments["repo"],
                    unit_id=arguments["unit_id"],
                    domain=arguments.get("domain", "auto"),
                    token_budget=arguments.get("token_budget"),
                    include_callers=arguments.get("include_callers", False),
                    output_format=arguments.get("output_format", "json"),
                    budget_strategy=arguments.get("budget_strategy", "most_relevant"),
                    include_budget_report=arguments.get("include_budget_report", False),
                    fqn=arguments.get("fqn"),
                    include_related=arguments.get("include_related", False),
                    strip_boilerplate=arguments.get("strip_boilerplate", False),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
        elif name == "index_repo":
            from .tools.content_router import index_content

            result = await asyncio.to_thread(
                functools.partial(
                    index_content,
                    url=arguments["url"],
                    domain="code",
                    use_ai_summaries=arguments.get(
                        "use_ai_summaries", _default_use_ai_summaries()
                    ),
                    incremental=arguments.get("incremental", True),
                    extra_ignore_patterns=arguments.get("extra_ignore_patterns"),
                    progress_cb=_progress_cb,
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
            _result_cache_invalidate()
        elif name == "index_folder":
            from .tools.content_router import index_content

            _ai = arguments.get("use_ai_summaries", _default_use_ai_summaries())
            result = await asyncio.to_thread(
                functools.partial(
                    index_content,
                    path=arguments["path"],
                    domain="code",
                    use_ai_summaries=_ai,
                    incremental=arguments.get("incremental", True),
                    extra_ignore_patterns=arguments.get("extra_ignore_patterns"),
                    follow_symlinks=arguments.get("follow_symlinks", False),
                    paths=arguments.get("paths"),
                    identity_mode=arguments.get("identity_mode", "config"),
                    progress_cb=_progress_cb,
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
            _result_cache_invalidate()
        elif name == "summarize_repo":
            from .tools.summarize_repo import summarize_repo

            result = await asyncio.to_thread(
                functools.partial(
                    summarize_repo,
                    repo=arguments["repo"],
                    force=arguments.get("force", False),
                    storage_path=storage_path,
                )
            )
        elif name == "index_file":
            from .tools.index_file import index_file

            _ai = arguments.get("use_ai_summaries", _default_use_ai_summaries())
            result = await asyncio.to_thread(
                functools.partial(
                    index_file,
                    path=arguments["path"],
                    use_ai_summaries=_ai,
                    storage_path=storage_path,
                    context_providers=arguments.get("context_providers", True),
                    progress_cb=_progress_cb,
                )
            )
            _result_cache_invalidate()
        elif name == "import_runtime_signal":
            from .tools.import_runtime_signal import import_runtime_signal

            result = await asyncio.to_thread(
                functools.partial(
                    import_runtime_signal,
                    source=arguments.get("source", "otel"),
                    path=arguments["path"],
                    repo=arguments.get("repo"),
                    redact_enabled=arguments.get("redact_enabled"),
                    storage_path=storage_path,
                )
            )
        elif name == "find_hot_paths":
            from .tools.find_hot_paths import find_hot_paths

            result = await asyncio.to_thread(
                functools.partial(
                    find_hot_paths,
                    repo=arguments["repo"],
                    query=arguments.get("query"),
                    top_n=arguments.get("top_n", 20),
                    storage_path=storage_path,
                )
            )
        elif name == "list_repos":
            from .tools.list_repos import list_repos

            result = await asyncio.to_thread(
                functools.partial(list_repos, storage_path=storage_path)
            )
        elif name == "resolve_repo":
            from .tools.resolve_repo import resolve_repo

            result = await asyncio.to_thread(
                functools.partial(
                    resolve_repo,
                    path=arguments["path"],
                    storage_path=storage_path,
                )
            )
        elif name == "get_file_tree":
            from .tools.content_router import list_content

            result = await asyncio.to_thread(
                functools.partial(
                    list_content,
                    repo=arguments["repo"],
                    domain="code",
                    path_prefix=arguments.get("path_prefix", ""),
                    include_summaries=arguments.get("include_summaries", False),
                    max_files=arguments.get("max_files"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
        elif name == "get_file_outline":
            from .tools.content_router import get_outline

            result = await asyncio.to_thread(
                functools.partial(
                    get_outline,
                    repo=arguments["repo"],
                    file_path=arguments.get("file_path") or arguments.get("file") or "",
                    file_paths=arguments.get("file_paths"),
                    domain="code",
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
        elif name == "get_file_content":
            from .tools.content_router import get_file

            result = await asyncio.to_thread(
                functools.partial(
                    get_file,
                    repo=arguments["repo"],
                    file_path=arguments["file_path"],
                    domain="code",
                    start_line=arguments.get("start_line"),
                    end_line=arguments.get("end_line"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
        elif name == "get_symbol_source":
            from .tools.content_router import get_unit

            result = await asyncio.to_thread(
                functools.partial(
                    get_unit,
                    repo=arguments["repo"],
                    unit_id=arguments.get("symbol_id"),
                    unit_ids=arguments.get("symbol_ids"),
                    domain="code",
                    verify=arguments.get("verify", False),
                    verify_against=arguments.get("verify_against", "cache"),
                    context_lines=arguments.get("context_lines", 0),
                    fqn=arguments.get("fqn"),
                    source_start_line=arguments.get("source_start_line"),
                    source_end_line=arguments.get("source_end_line"),
                    max_source_lines=arguments.get("max_source_lines"),
                    max_source_bytes=arguments.get("max_source_bytes"),
                    max_total_source_bytes=arguments.get("max_total_source_bytes"),
                    storage_path=storage_path,
                    doc_storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
        elif name == "search_symbols":
            from .tools.content_router import search_units

            kind_filter = arguments.get("kind")
            if kind_filter and kind_filter not in VALID_KINDS:
                result = {
                    "error": f"Unknown kind '{kind_filter}'. Valid values: {sorted(VALID_KINDS)}"
                }
            else:
                result = await asyncio.to_thread(
                    functools.partial(
                        search_units,
                        query=arguments["query"],
                        repo=arguments["repo"],
                        domain="code",
                        kind=kind_filter,
                        file_pattern=arguments.get("file_pattern"),
                        language=arguments.get("language"),
                        decorator=arguments.get("decorator"),
                        max_results=arguments.get("max_results", 10),
                        token_budget=arguments.get("token_budget"),
                        detail_level=arguments.get("detail_level", "standard"),
                        debug=arguments.get("debug", False),
                        fuzzy=arguments.get("fuzzy", False),
                        fuzzy_threshold=arguments.get("fuzzy_threshold", 0.4),
                        max_edit_distance=arguments.get("max_edit_distance", 2),
                        sort_by=arguments.get("sort_by", "relevance"),
                        semantic=arguments.get("semantic", False),
                        semantic_weight=arguments.get("semantic_weight", 0.5),
                        semantic_only=arguments.get("semantic_only", False),
                        fusion=arguments.get("fusion", False),
                        fqn=arguments.get("fqn"),
                        storage_path=storage_path,
                        doc_storage_path=storage_path,
                    )
                )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
        elif name == "search_text":
            from .tools.search_text import search_text

            result = await asyncio.to_thread(
                functools.partial(
                    search_text,
                    repo=arguments["repo"],
                    query=arguments["query"],
                    file_pattern=arguments.get("file_pattern"),
                    max_results=arguments.get("max_results", 20),
                    context_lines=arguments.get("context_lines", 0),
                    is_regex=arguments.get("is_regex", False),
                    storage_path=storage_path,
                )
            )
        elif name == "find_references":
            from .tools.find_references import find_references

            result = await asyncio.to_thread(
                functools.partial(
                    find_references,
                    repo=arguments["repo"],
                    identifier=arguments.get("identifier"),
                    identifiers=arguments.get("identifiers"),
                    max_results=arguments.get("max_results", 20),
                    include_call_chain=arguments.get("include_call_chain", False),
                    storage_path=storage_path,
                )
            )
        elif name == "search_columns":
            from .tools.search_columns import search_columns

            result = await asyncio.to_thread(
                functools.partial(
                    search_columns,
                    repo=arguments["repo"],
                    query=arguments["query"],
                    model_pattern=arguments.get("model_pattern"),
                    max_results=arguments.get("max_results", 20),
                    storage_path=storage_path,
                )
            )
        elif name == "get_context_bundle":
            from .tools.get_context_bundle import get_context_bundle

            result = await asyncio.to_thread(
                functools.partial(
                    get_context_bundle,
                    repo=arguments["repo"],
                    symbol_id=arguments.get("symbol_id"),
                    symbol_ids=arguments.get("symbol_ids"),
                    token_budget=arguments.get("token_budget"),
                    include_callers=arguments.get("include_callers", False),
                    output_format=arguments.get("output_format", "json"),
                    budget_strategy=arguments.get("budget_strategy", "most_relevant"),
                    include_budget_report=arguments.get("include_budget_report", False),
                    fqn=arguments.get("fqn"),
                    storage_path=storage_path,
                )
            )
            # Unwrap the single-domain wrapper so old callers see the native shape
            if isinstance(result, dict) and result.get("results"):
                result = result["results"][0].get("result", result)
        elif name == "assemble_task_context":
            from .tools.assemble_task_context import assemble_task_context

            result = await asyncio.to_thread(
                functools.partial(
                    assemble_task_context,
                    repo=arguments["repo"],
                    task=arguments["task"],
                    symbols=arguments.get("symbols"),
                    intent=arguments.get("intent"),
                    token_budget=arguments.get("token_budget", 8000),
                    include=arguments.get("include"),
                    cross_repo=arguments.get("cross_repo", False),
                    storage_path=storage_path,
                )
            )
        elif name == "register_edit":
            from .tools.register_edit import register_edit

            result = await asyncio.to_thread(
                functools.partial(
                    register_edit,
                    repo=arguments["repo"],
                    file_paths=arguments["file_paths"],
                    reindex=arguments.get("reindex", False),
                    storage_path=storage_path,
                )
            )
        elif name == "get_dependency_graph":
            from .tools.get_dependency_graph import get_dependency_graph

            result = await asyncio.to_thread(
                functools.partial(
                    get_dependency_graph,
                    repo=arguments["repo"],
                    file=arguments["file"],
                    direction=arguments.get("direction", "imports"),
                    depth=arguments.get("depth", 1),
                    storage_path=storage_path,
                    cross_repo=arguments.get("cross_repo"),
                )
            )
        elif name == "get_blast_radius":
            from .tools.get_blast_radius import get_blast_radius

            result = await asyncio.to_thread(
                functools.partial(
                    get_blast_radius,
                    repo=arguments["repo"],
                    symbol=arguments["symbol"],
                    depth=arguments.get("depth", 1),
                    include_depth_scores=arguments.get("include_depth_scores", False),
                    storage_path=storage_path,
                    cross_repo=arguments.get("cross_repo"),
                    call_depth=arguments.get("call_depth", 0),
                    fqn=arguments.get("fqn"),
                    decorator_filter=arguments.get("decorator_filter"),
                    include_source=arguments.get("include_source", False),
                    source_budget=arguments.get("source_budget", 8000),
                    include_decisions=arguments.get("include_decisions", False),
                )
            )
        elif name == "get_call_hierarchy":
            from .tools.get_call_hierarchy import get_call_hierarchy

            chains = arguments.get("chains", False)

            result = await asyncio.to_thread(
                functools.partial(
                    get_call_hierarchy,
                    repo=arguments["repo"],
                    symbol_id=arguments["symbol_id"],
                    direction=arguments.get("direction", "both"),
                    depth=arguments.get("depth", 3),
                    storage_path=storage_path,
                    include_impact=arguments.get("include_impact", False),
                    include_decisions=arguments.get("include_decisions", False),
                    chains=chains,
                )
            )
        elif name == "get_symbol_provenance":
            from .tools.get_symbol_provenance import get_symbol_provenance

            result = await asyncio.to_thread(
                functools.partial(
                    get_symbol_provenance,
                    repo=arguments["repo"],
                    symbol=arguments["symbol"],
                    max_commits=arguments.get("max_commits", 25),
                    storage_path=storage_path,
                )
            )
        elif name == "get_pr_risk_profile":
            from .tools.get_pr_risk_profile import get_pr_risk_profile

            result = await asyncio.to_thread(
                functools.partial(
                    get_pr_risk_profile,
                    repo=arguments["repo"],
                    base_ref=arguments.get("base_ref"),
                    head_ref=arguments.get("head_ref", "HEAD"),
                    days=arguments.get("days", 90),
                    storage_path=storage_path,
                )
            )
        elif name == "check_safe":
            from .tools.check_safe import check_safe

            result = await asyncio.to_thread(
                functools.partial(
                    check_safe,
                    repo=arguments["repo"],
                    symbol=arguments["symbol"],
                    mode=arguments.get("mode", "delete"),
                    cross_repo=arguments.get("cross_repo", True),
                    include_runtime=arguments.get("include_runtime", True),
                    storage_path=storage_path,
                )
            )
        elif name == "find_implementations":
            from .tools.find_implementations import find_implementations

            result = await asyncio.to_thread(
                functools.partial(
                    find_implementations,
                    repo=arguments["repo"],
                    symbol=arguments["symbol"],
                    relationship_kinds=arguments.get("relationship_kinds"),
                    include_subclasses=arguments.get("include_subclasses", True),
                    cross_repo=arguments.get("cross_repo", False),
                    rank_by_importance=arguments.get("rank_by_importance", True),
                    max_results=arguments.get("max_results", 50),
                    token_budget=arguments.get("token_budget", 4000),
                    storage_path=storage_path,
                )
            )
        elif name == "plan_refactoring":
            from .tools.plan_refactoring import plan_refactoring

            result = await asyncio.to_thread(
                functools.partial(
                    plan_refactoring,
                    repo=arguments["repo"],
                    symbol=arguments["symbol"],
                    refactor_type=arguments["refactor_type"],
                    new_name=arguments.get("new_name"),
                    new_file=arguments.get("new_file"),
                    new_signature=arguments.get("new_signature"),
                    depth=arguments.get("depth", 2),
                    storage_path=storage_path,
                )
            )
        elif name == "get_dead_code_v2":
            from .tools.get_dead_code_v2 import get_dead_code_v2

            result = await asyncio.to_thread(
                functools.partial(
                    get_dead_code_v2,
                    repo=arguments["repo"],
                    min_confidence=arguments.get("min_confidence", 0.5),
                    include_tests=arguments.get("include_tests", False),
                    max_results=arguments.get("max_results", 100),
                    file_pattern=arguments.get("file_pattern"),
                    storage_path=storage_path,
                )
            )
        elif name == "get_symbol_complexity":
            from .tools.get_symbol_complexity import get_symbol_complexity

            result = await asyncio.to_thread(
                functools.partial(
                    get_symbol_complexity,
                    repo=arguments["repo"],
                    symbol_id=arguments["symbol_id"],
                    storage_path=storage_path,
                )
            )
        elif name == "get_repo_health":
            from .tools.get_repo_health import get_repo_health

            result = await asyncio.to_thread(
                functools.partial(
                    get_repo_health,
                    repo=arguments["repo"],
                    days=arguments.get("days", 90),
                    detailed=arguments.get("detailed", False),
                    file_path=arguments.get("file_path"),
                    rules=arguments.get("rules"),
                    top_n=arguments.get("top_n", 20),
                    min_confidence=arguments.get("min_confidence", 0.5),
                    max_results=arguments.get("max_results", 100),
                    storage_path=storage_path,
                )
            )
        elif name == "get_class_hierarchy":
            from .tools.get_class_hierarchy import get_class_hierarchy

            result = await asyncio.to_thread(
                functools.partial(
                    get_class_hierarchy,
                    repo=arguments["repo"],
                    class_name=arguments["class_name"],
                    storage_path=storage_path,
                )
            )
        elif name == "get_repo_map":
            mode = arguments.get("mode", "map")

            if mode == "outline":
                from .tools.get_repo_map import get_repo_map

                result = await asyncio.to_thread(
                    functools.partial(
                        get_repo_map,
                        repo=arguments["repo"],
                        mode="outline",
                        storage_path=storage_path,
                    )
                )
            else:
                from .tools.get_repo_map import get_repo_map

                result = await asyncio.to_thread(
                    functools.partial(
                        get_repo_map,
                        repo=arguments["repo"],
                        token_budget=arguments.get("token_budget", 2048),
                        scope=arguments.get("scope"),
                        max_per_file=arguments.get("max_per_file", 5),
                        include_kinds=arguments.get("include_kinds"),
                        storage_path=storage_path,
                        group_by=arguments.get("group_by", "file"),
                        algorithm=arguments.get("algorithm", "pagerank"),
                        top_n=arguments.get("top_n", 20),
                    )
                )
        elif name == "find_similar_symbols":
            from .tools.find_similar_symbols import find_similar_symbols

            result = await asyncio.to_thread(
                functools.partial(
                    find_similar_symbols,
                    repo=arguments["repo"],
                    threshold=arguments.get("threshold", 0.80),
                    min_size=arguments.get("min_size", 30),
                    max_clusters=arguments.get("max_clusters", 25),
                    include_tests=arguments.get("include_tests", False),
                    scope=arguments.get("scope"),
                    include_kinds=arguments.get("include_kinds"),
                    semantic_weight=arguments.get("semantic_weight", 0.6),
                    token_budget=arguments.get("token_budget", 4000),
                    storage_path=storage_path,
                )
            )
        elif name == "search_ast":
            from .tools.search_ast import search_ast

            result = await asyncio.to_thread(
                functools.partial(
                    search_ast,
                    repo=arguments["repo"],
                    pattern=arguments.get("pattern"),
                    category=arguments.get("category"),
                    language=arguments.get("language"),
                    file_pattern=arguments.get("file_pattern"),
                    max_results=arguments.get("max_results", 50),
                    storage_path=storage_path,
                )
            )
        elif name == "get_changed_symbols":
            from .tools.get_changed_symbols import get_changed_symbols

            result = await asyncio.to_thread(
                functools.partial(
                    get_changed_symbols,
                    repo=arguments["repo"],
                    since_sha=arguments.get("since_sha"),
                    until_sha=arguments.get("until_sha", "HEAD"),
                    include_blast_radius=arguments.get("include_blast_radius", False),
                    max_blast_depth=arguments.get("max_blast_depth", 3),
                    storage_path=storage_path,
                )
            )
        elif name == "embed_repo":
            from .tools.embed_repo import embed_repo

            result = await asyncio.to_thread(
                functools.partial(
                    embed_repo,
                    repo=arguments["repo"],
                    batch_size=arguments.get("batch_size", 50),
                    force=arguments.get("force", False),
                    storage_path=storage_path,
                    progress_cb=_progress_cb,
                )
            )
        elif name == "get_tectonic_map":
            from .tools.get_tectonic_map import get_tectonic_map

            result = await asyncio.to_thread(
                functools.partial(
                    get_tectonic_map,
                    repo=arguments["repo"],
                    days=arguments.get("days", 90),
                    min_plate_size=arguments.get("min_plate_size", 2),
                    storage_path=storage_path,
                )
            )
        elif name == "list_workspaces":
            from .tools.list_workspaces import list_workspaces

            result = await asyncio.to_thread(
                functools.partial(
                    list_workspaces,
                    repo=arguments["repo"],
                    storage_path=storage_path,
                )
            )
        elif name == "get_project_intel":
            from .tools.get_project_intel import get_project_intel

            result = await asyncio.to_thread(
                functools.partial(
                    get_project_intel,
                    repo=arguments["repo"],
                    category=arguments.get("category", "all"),
                    scope_path=arguments.get("scope_path"),
                    storage_path=storage_path,
                )
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

        # Feature 2: Session journal recording
        if config_module.get("session_journal", True):
            try:
                from .tools.session_journal import get_journal

                journal = get_journal()
                journal.record_tool_call(name)
                # Record file reads for relevant tools
                if name in {
                    "get_file_content",
                    "get_file_outline",
                    "get_symbol_source",
                    "get_context_bundle",
                }:
                    if isinstance(result, dict):
                        # Extract file paths from result
                        if name == "get_file_content" and "content" in result:
                            journal.record_read(arguments.get("file_path", ""), name)
                        elif name == "get_file_outline" and "symbols" in result:
                            journal.record_read(arguments.get("file_path", ""), name)
                        elif name == "get_symbol_source":
                            # Single symbol_id → flat result with "source"
                            sym_id = arguments.get("symbol_id", "")
                            if sym_id and "::" in sym_id:
                                journal.record_read(sym_id.split("::")[0], name)
                            # Batch symbol_ids → result has "symbols" list
                            for sym in result.get("symbols", []):
                                if "file" in sym:
                                    journal.record_read(sym["file"], name)
                        elif name == "get_context_bundle" and "symbols" in result:
                            # Record all files from the bundle
                            for sym in result.get("symbols", []):
                                if "file" in sym:
                                    journal.record_read(sym["file"], name)
                # Record searches
                elif name in {"search_symbols", "search_text"}:
                    if isinstance(result, dict):
                        result_count = result.get("result_count", 0)
                        query = arguments.get("query", "")
                        if query:
                            journal.record_search(query, result_count)
                        # Collect negative evidence for session state persistence
                        ne = result.get("negative_evidence")
                        if ne and isinstance(ne, dict):
                            import time as _t

                            journal.record_negative_evidence(
                                {
                                    "query": query,
                                    "repo": arguments.get("repo", ""),
                                    "verdict": ne.get("verdict", ""),
                                    "scanned_symbols": ne.get("scanned_symbols", 0),
                                    "timestamp": _t.time(),
                                }
                            )
                _maybe_flush_live_journal(journal)
            except Exception:
                logger.debug("Journal recording failed", exc_info=True)

        # Feature 7: Turn budget — record output and inject warnings
        try:
            budget_tokens = config_module.get("turn_budget_tokens", 20000)
            if budget_tokens > 0 and isinstance(result, dict):
                from .tools.turn_budget import get_turn_budget

                tb = get_turn_budget()
                # Reconfigure if config changed (thread-safe)
                tb.configure(budget_tokens, config_module.get("turn_gap_seconds", 30.0))
                # Auto-compact: downgrade detail_level before dispatch would be ideal,
                # but result is already computed. Inject warning + flag instead.
                result_bytes = len(json.dumps(result, default=str))
                token_count = result_bytes // 4  # ~4 bytes per token
                budget_info = tb.record_output(token_count)
                if budget_info.get("budget_warning"):
                    meta = result.setdefault("_meta", {})
                    meta["budget_warning"] = budget_info["budget_warning"]
                    meta["turn_tokens_used"] = budget_info["turn_tokens_used"]
                    meta["turn_budget_remaining"] = budget_info["turn_budget_remaining"]
                    if tb.should_compact():
                        meta["auto_compacted"] = True
                    # Also promote to top-level for visibility
                    result["budget_warning"] = budget_info["budget_warning"]
            elif budget_tokens > 0:
                # Still record token count for non-dict results (errors, etc.)
                from .tools.turn_budget import get_turn_budget

                tb = get_turn_budget()
                tb.configure(budget_tokens, config_module.get("turn_gap_seconds", 30.0))
                # Approximate token count for non-dict results
                tb.record_output(len(json.dumps(result, default=str)) // 4)
        except Exception:
            logger.debug("Turn budget recording failed", exc_info=True)

        # Agent Selector: score complexity and annotate result
        try:
            agent_selector_cfg = config_module.get("agent_selector", {})
            if (
                isinstance(agent_selector_cfg, dict)
                and agent_selector_cfg.get("mode", "off") != "off"
            ):
                if (
                    isinstance(result, dict)
                    and "error" not in result
                    and name in _AGENT_SELECTOR_TOOLS
                ):
                    from .agent_selector import (
                        AgentSelectorConfig,
                        ComplexitySignals,
                        route,
                        score_complexity,
                    )

                    as_config = AgentSelectorConfig.from_config(agent_selector_cfg)
                    # Build signals from result metadata
                    signals = ComplexitySignals(
                        retrievalSetSize=result.get(
                            "items_included", result.get("symbol_count", 0)
                        ),
                        symbolCount=result.get(
                            "symbol_count",
                            len(result.get("symbols", result.get("context_items", []))),
                        ),
                        crossFileReferences=result.get("cross_file_refs", 0),
                        crossProjectReferences=result.get("cross_project", False),
                        languageComplexity=result.get(
                            "language_complexity", "standard"
                        ),
                        requestTokenEstimate=result.get(
                            "used_tokens", result.get("total_tokens", 0)
                        ),
                    )
                    assessment = score_complexity(signals, as_config)
                    current_model = arguments.get("_current_model")
                    decision = route(assessment, as_config, current_model)
                    # Annotate result
                    meta = result.setdefault("_meta", {})
                    meta["agent_selector"] = {
                        "score": assessment.score,
                        "tier": assessment.tier,
                        "recommendedModel": assessment.recommendedModel,
                    }
                    if decision.prompt_text:
                        result["agent_selector_prompt"] = decision.prompt_text
                    if decision.metadata_text:
                        result["agent_selector"] = decision.metadata_text
        except Exception:
            logger.debug("Agent selector scoring failed", exc_info=True)

        if isinstance(result, dict):
            meta_fields = config_module.get("meta_fields")
            if meta_fields == [] or arguments.get("suppress_meta"):
                result.pop("_meta", None)
                # Also strip nested _meta from batch tools (e.g. get_file_outline batch)
                for _item in result.get("results", []):
                    if isinstance(_item, dict):
                        _item.pop("_meta", None)
            elif isinstance(meta_fields, list):
                # Partial field inclusion — keep only the fields listed in meta_fields,
                # preserving tool-generated fields (timing_ms, tokens_saved, etc.)
                existing_meta = result.pop("_meta", {})
                _meta: dict[str, Any] = {}
                if "powered_by" in meta_fields:
                    _meta["powered_by"] = (
                        "jcodemunch-mcp by jgravelle · https://github.com/jgravelle/jcodemunch-mcp"
                    )
                for field in meta_fields:
                    if field in existing_meta:
                        _meta[field] = existing_meta[field]
                if _meta:
                    result["_meta"] = _meta
                # Also filter nested _meta from batch tools (e.g. get_file_outline batch)
                for _item in result.get("results", []):
                    if isinstance(_item, dict):
                        _item_meta = _item.pop("_meta", {})
                        _item_filtered: dict[str, Any] = {
                            f: _item_meta[f] for f in meta_fields if f in _item_meta
                        }
                        if "powered_by" in meta_fields:
                            _item_filtered["powered_by"] = (
                                "jcodemunch-mcp by jgravelle · https://github.com/jgravelle/jcodemunch-mcp"
                            )
                        if _item_filtered:
                            _item["_meta"] = _item_filtered
        # Per-call pulse for downstream consumers (dashboards, monitors)
        _saved = (
            result.get("_meta", {}).get("tokens_saved", 0)
            if isinstance(result, dict)
            else 0
        )
        _write_pulse(name, tokens_saved=_saved, base_path=storage_path)

        # Response-level secret redaction — scrub leaked credentials
        # before they reach the LLM context window. Skipped for tools that
        # return raw cached source (any "secret" found is the user's own
        # checked-in code; the per-byte regex sweep is wasted latency on
        # tools whose payloads can be hundreds of KB).
        _SOURCE_DUMP_TOOLS = frozenset(
            {
                "get_file_content",
                "get_symbol_source",
                "get_context_bundle",
            }
        )
        if isinstance(result, dict) and name not in _SOURCE_DUMP_TOOLS:
            try:
                from .redact import is_redaction_enabled, redact_dict

                if is_redaction_enabled():
                    result, _redact_count = redact_dict(result)
                    if _redact_count > 0:
                        meta = result.setdefault("_meta", {})
                        meta["secrets_redacted"] = _redact_count
            except Exception:
                logger.debug("Secret redaction failed", exc_info=True)

        # Compact output encoding (MUNCH). Opt-in via `format` argument or
        # JCODEMUNCH_DEFAULT_FORMAT env; "auto" falls back to JSON unless
        # savings clear the gate threshold.
        try:
            from .encoding import encode_response
            from .storage.token_tracker import record_encoding_savings

            encoded, enc_meta = encode_response(
                name, result, _requested_format, repo=repo_arg
            )
            if enc_meta.get("encoding") != "json":
                saved = enc_meta.get("encoding_tokens_saved", 0)
                total_enc = record_encoding_savings(
                    saved, base_path=storage_path, tool_name=name
                )
                if isinstance(result, dict):
                    m = result.setdefault("_meta", {})
                    m["encoding"] = enc_meta["encoding"]
                    m["encoding_tokens_saved"] = saved
                    m["total_encoding_tokens_saved"] = total_enc
                return [TextContent(type="text", text=encoded)]
        except Exception:
            logger.debug("Compact encoding failed; emitting JSON", exc_info=True)

        _text = json.dumps(result, separators=(",", ":"))
        if isinstance(result, dict) and "error" in result:
            # In-band tool error (e.g. ambiguous/not-found repo, Unknown tool).
            # Carry the same JSON body but flag isError for clients that branch
            # on it (F-P01); the v1.108.30 passthrough already kept errors JSON.
            _call_ok = False
            return _error_call_result(_text)
        return [TextContent(type="text", text=_text)]

    except KeyError as e:
        _call_ok = False
        # A KeyError raised while extracting arguments in THIS dispatcher frame is
        # a genuine missing caller argument. A KeyError raised deeper — inside a
        # tool implementation (e.g. a dict-shape bug) — must NOT masquerade as a
        # schema/argument problem (#331). Distinguish by the originating frame.
        _tb = e.__traceback__
        while _tb is not None and _tb.tb_next is not None:
            _tb = _tb.tb_next
        _origin = _tb.tb_frame.f_code.co_filename if _tb is not None else ""
        if _origin and os.path.basename(_origin) != os.path.basename(__file__):
            logger.error(
                "call_tool %s raised an internal KeyError", name, exc_info=True
            )
            payload = {
                "error": f"Internal error processing {name}",
                "summary": f"KeyError: {e}",
            }
            return _error_call_result(json.dumps(payload, separators=(",", ":")))
        return _error_call_result(
            json.dumps(
                {
                    "error": f"Missing required argument: {e}. Check the tool schema for correct parameter names."
                },
                separators=(",", ":"),
            )
        )
    except Exception as exc:
        _call_ok = False
        logger.error("call_tool %s failed", name, exc_info=True)
        summary = " ".join((str(exc).strip().splitlines() or [""])[0].split())
        summary = f"{type(exc).__name__}: {summary}" if summary else type(exc).__name__
        if len(summary) > 200:
            summary = f"{summary[:197].rstrip()}..."
        payload = {
            "error": f"Internal error processing {name}",
            "summary": summary,
        }
        return _error_call_result(json.dumps(payload, separators=(",", ":")))
    finally:
        try:
            from .storage.token_tracker import record_tool_latency

            duration_ms = (time.perf_counter() - _t0_call) * 1000.0
            _repo_arg = arguments.get("repo") if isinstance(arguments, dict) else None
            record_tool_latency(name, duration_ms, ok=_call_ok, repo=_repo_arg)
        except Exception:
            logger.debug("Latency recording failed for %s", name, exc_info=True)


async def _run_server_with_watcher(
    server_coro_func,
    server_args: tuple,
    watcher_kwargs: dict,
    log_path: Optional[str] = None,
) -> None:
    """Run MCP server with a background watcher in the same event loop.

    Watcher runs in quiet mode (no stderr output). If log_path is provided,
    watcher output and errors go to that file. If log_path is "auto", a temp
    file is created in the system temp directory.
    """
    global _watcher_manager

    if watch_folders is None or WatcherManager is None:
        raise ImportError(
            "watchfiles is required for --watcher. "
            "Install with: pip install 'jcodemunch-mcp[watch]'"
        )

    import tempfile

    # Resolve log file path
    if log_path == "auto":
        log_path = os.path.join(
            tempfile.gettempdir(),
            f"jcw_{os.getpid()}.log",
        )

    stop_event = asyncio.Event()

    _log_path = log_path

    # Open log file handle if provided
    _log_file_handle: Optional[TextIO] = None
    if _log_path:
        try:
            _log_file_handle = open(_log_path, "a", encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not open watcher log %r: %s — continuing without log",
                _log_path,
                exc,
            )
            _log_file_handle = None

    # Create WatcherManager and add initial paths
    manager = WatcherManager(
        debounce_ms=watcher_kwargs.get("debounce_ms", 200),
        use_ai_summaries=watcher_kwargs.get("use_ai_summaries", True),
        storage_path=watcher_kwargs.get("storage_path"),
        extra_ignore_patterns=watcher_kwargs.get("extra_ignore_patterns"),
        follow_symlinks=watcher_kwargs.get("follow_symlinks", False),
        quiet=True,
        log_file_handle=_log_file_handle,
    )
    manager._stop_event = stop_event

    # Add initial paths
    initial_paths = watcher_kwargs.get("paths", [])
    for path in initial_paths:
        folder = Path(path).expanduser().resolve()
        if folder.is_dir():
            await manager.add_folder(str(folder))

    _watcher_manager = manager

    # Create manager run task (self-restarts on crash)
    manager_task = asyncio.create_task(
        manager.run(),
        name="watcher-manager",
    )

    try:
        await server_coro_func(*server_args)
    except asyncio.CancelledError:
        pass  # Clean shutdown via Ctrl+C
    finally:
        _watcher_manager = None
        stop_event.set()
        # Remove all folders
        for folder in list(manager._watched):
            await manager.remove_folder(folder)
        manager.stop()
        manager_task.cancel()
        try:
            await asyncio.wait_for(manager_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            manager_task.cancel()
            try:
                await manager_task
            except asyncio.CancelledError:
                pass
        except (WatcherError, Exception) as exc:
            logger.warning("Watcher stopped with error: %s", exc)
        # Close log file handle
        if _log_file_handle is not None:
            try:
                _log_file_handle.close()
            except Exception:
                pass
        from .storage import IndexStore

        IndexStore(
            base_path=watcher_kwargs.get("storage_path")
            or os.environ.get("CODE_INDEX_PATH")
        ).close()


def _init_error_tracking_for_startup() -> None:
    """Initialize optional error-tracking SDKs for every server transport."""
    from .error_tracking import init_error_tracking

    init_error_tracking()


async def run_stdio_server():
    """Run the MCP server over stdio (default)."""
    import sys

    from mcp.server.stdio import stdio_server

    _init_error_tracking_for_startup()

    print(
        f"jcodemunch-mcp {__version__} by jgravelle · https://github.com/jgravelle/jcodemunch-mcp",
        file=sys.stderr,
    )
    logger.info(
        "startup version=%s transport=stdio storage=%s ai_summaries=%s",
        __version__,
        os.path.expanduser(os.environ.get("CODE_INDEX_PATH", "~/.code-index/")),
        _default_use_ai_summaries(),
    )
    # Version-drift probe: on first launch after upgrade, emit a one-line
    # hint pointing at the release notes. Silent on first-ever launch and
    # on any OS-level failure.
    try:
        from .version_check import check_and_announce

        check_and_announce()
    except Exception:
        logger.debug("version_check probe failed", exc_info=True)
    # Feature 10: Restore session state on startup
    _restore_session_state()

    # Handshake watchdog. If the client never reaches any of our MCP
    # handlers (list_tools, list_resources, list_prompts, get_prompt,
    # call_tool) within JCODEMUNCH_HANDSHAKE_TIMEOUT seconds, write a
    # one-line stderr hint. This catches stdio-channel corruption — the
    # paying-client report against Codex/rmcp where uvx chatter on stdout
    # made the client wait 5h+ for a frame that was never coming. Set
    # JCODEMUNCH_HANDSHAKE_TIMEOUT=0 to disable.
    global _handshake_event
    _handshake_event = asyncio.Event()
    try:
        _handshake_timeout = float(os.environ.get("JCODEMUNCH_HANDSHAKE_TIMEOUT", "5"))
    except (ValueError, TypeError):
        _handshake_timeout = 5.0

    async def _handshake_watchdog() -> None:
        if _handshake_timeout <= 0:
            return
        try:
            await asyncio.wait_for(_handshake_event.wait(), timeout=_handshake_timeout)
        except asyncio.TimeoutError:
            sys.stderr.write(
                f"[jcodemunch-mcp] handshake not completed after "
                f"{_handshake_timeout:.0f}s — the client has not called any MCP "
                f"handler. If you spawn this server via `uvx`, stdout chatter "
                f"from package resolution can corrupt the JSON-RPC channel for "
                f"strict clients (notably Codex/rmcp). Workarounds: "
                f"(1) install the binary with `pip install jcodemunch-mcp` and "
                f"point your client at it directly, or (2) set UV_NO_PROGRESS=1 "
                f"UV_QUIET=1 in the spawn env. Set JCODEMUNCH_HANDSHAKE_TIMEOUT=0 "
                f"to silence this warning.\n"
            )
            sys.stderr.flush()
        except asyncio.CancelledError:
            pass

    _watchdog_task = asyncio.create_task(_handshake_watchdog())

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if not _watchdog_task.done():
            _watchdog_task.cancel()
        from .storage import IndexStore

        IndexStore(base_path=os.environ.get("CODE_INDEX_PATH")).close()


def _make_auth_middleware():
    """Return a Starlette middleware class that checks JCODEMUNCH_HTTP_TOKEN if set."""
    token = os.environ.get("JCODEMUNCH_HTTP_TOKEN")
    if not token:
        return None

    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                return JSONResponse(
                    {
                        "error": "Unauthorized. Set Authorization: Bearer <JCODEMUNCH_HTTP_TOKEN> header."
                    },
                    status_code=401,
                )
            return await call_next(request)

    return Middleware(BearerAuthMiddleware)


def _make_rate_limit_middleware():
    """Return a Starlette middleware that rate-limits by IP (optional, opt-in).

    Reads JCODEMUNCH_RATE_LIMIT env var.  Value is max requests per minute per
    client IP.  0 or unset disables rate limiting (default — no behaviour change
    for existing deployments).

    Returns a Middleware instance, or None when rate limiting is disabled.
    """
    try:
        limit = int(os.environ.get("JCODEMUNCH_RATE_LIMIT", "0"))
    except (ValueError, TypeError):
        limit = 0
    if limit <= 0:
        return None

    import collections
    import time as _time

    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    _WINDOW = 60.0  # seconds
    _buckets: dict[str, collections.deque] = {}

    # Hard cap on tracked IPs so a botnet/rotating-NAT client cannot bloat
    # the bucket dict indefinitely. When full, evict the oldest-touched entry.
    _MAX_TRACKED_IPS = 10_000
    _last_touched: dict[str, float] = {}

    class RateLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            ip = request.client.host if request.client else "unknown"
            now = _time.monotonic()
            bucket = _buckets.setdefault(ip, collections.deque())
            _last_touched[ip] = now
            # Evict timestamps outside the sliding window
            while bucket and now - bucket[0] >= _WINDOW:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = int(_WINDOW - (now - bucket[0])) + 1
                return JSONResponse(
                    {
                        "error": f"Rate limit exceeded. Max {limit} requests per minute per IP."
                    },
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)
            # If the bucket is now empty after window-eviction, drop the IP
            # entry entirely so cold IPs don't accumulate forever.
            if not bucket:
                _buckets.pop(ip, None)
                _last_touched.pop(ip, None)
            elif len(_buckets) > _MAX_TRACKED_IPS:
                # Cap exceeded: evict the least-recently-touched IP.
                oldest_ip = min(_last_touched, key=_last_touched.get)
                _buckets.pop(oldest_ip, None)
                _last_touched.pop(oldest_ip, None)
            return await call_next(request)

    return Middleware(RateLimitMiddleware)


async def run_sse_server(host: str, port: int):
    """Run the MCP server with SSE transport (persistent HTTP mode)."""
    import sys

    _init_error_tracking_for_startup()

    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.routing import Mount, Route
    except ImportError as e:
        raise ImportError(
            f"SSE transport requires additional packages: {e}. "
            'Install them with: pip install "jcodemunch-mcp[http]"'
        ) from e
    from mcp.server.sse import SseServerTransport

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    middleware = []
    auth_mw = _make_auth_middleware()
    if auth_mw:
        middleware.append(auth_mw)
    rate_mw = _make_rate_limit_middleware()
    if rate_mw:
        middleware.append(rate_mw)

    # Phase 6: optional /runtime/* live-ingest routes (off by default; gated
    # by runtime_ingest_enabled config + JCODEMUNCH_HTTP_TOKEN auth).
    from .org.http_routes import make_org_routes
    from .runtime.http_routes import make_runtime_routes

    runtime_routes = make_runtime_routes()
    org_routes = make_org_routes()

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
            *runtime_routes,
            *org_routes,
        ],
        middleware=middleware,
    )

    print(
        f"jcodemunch-mcp {__version__} by jgravelle · SSE server at http://{host}:{port}/sse",
        file=sys.stderr,
    )
    if not os.environ.get("JCODEMUNCH_HTTP_TOKEN") and host not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        print(
            f"WARNING: SSE bound to non-loopback host {host!r} without "
            f"JCODEMUNCH_HTTP_TOKEN — anyone on the network can drive this MCP server. "
            f"Set JCODEMUNCH_HTTP_TOKEN to require bearer auth.",
            file=sys.stderr,
        )
    logger.info(
        "startup version=%s transport=sse host=%s port=%d storage=%s",
        __version__,
        host,
        port,
        os.path.expanduser(os.environ.get("CODE_INDEX_PATH", "~/.code-index/")),
    )
    # Feature 10: Restore session state on startup
    _restore_session_state()
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


async def run_streamable_http_server(host: str, port: int):
    """Run the MCP server with streamable-http transport (persistent HTTP mode)."""
    import sys
    import uuid

    _init_error_tracking_for_startup()

    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.routing import Route
    except ImportError as e:
        raise ImportError(
            f"Streamable-http transport requires additional packages: {e}. "
            'Install them with: pip install "jcodemunch-mcp[http]"'
        ) from e
    from mcp.server.streamable_http import (
        MCP_SESSION_ID_HEADER,
        StreamableHTTPServerTransport,
    )

    # Session registry: session_id -> (transport, background_task)
    # Keeps server.run() alive across multiple HTTP requests from the same client.
    _sessions: dict[str, StreamableHTTPServerTransport] = {}
    _session_tasks: dict[str, asyncio.Task[Any]] = {}
    _session_last_seen: dict[str, float] = {}

    # Resource caps. A misbehaving or hostile client must not be able to
    # balloon process memory by opening sessions and never sending DELETE.
    try:
        _MAX_SESSIONS = int(os.environ.get("JCODEMUNCH_MAX_SESSIONS", "1024"))
    except (ValueError, TypeError):
        _MAX_SESSIONS = 1024
    try:
        _SESSION_IDLE_TIMEOUT = float(
            os.environ.get("JCODEMUNCH_SESSION_IDLE_TIMEOUT", "300")
        )
    except (ValueError, TypeError):
        _SESSION_IDLE_TIMEOUT = 300.0

    def _drop_session(sid: str) -> None:
        _sessions.pop(sid, None)
        _session_last_seen.pop(sid, None)
        t = _session_tasks.pop(sid, None)
        if t and not t.done():
            t.cancel()

    async def _idle_session_sweeper() -> None:
        import time as _t

        try:
            while True:
                await asyncio.sleep(max(30.0, _SESSION_IDLE_TIMEOUT / 4))
                now = _t.monotonic()
                stale = [
                    sid
                    for sid, ts in list(_session_last_seen.items())
                    if now - ts > _SESSION_IDLE_TIMEOUT
                ]
                for sid in stale:
                    logger.info(
                        "evicting idle MCP session %s (idle > %.0fs)",
                        sid,
                        _SESSION_IDLE_TIMEOUT,
                    )
                    _drop_session(sid)
        except asyncio.CancelledError:
            pass

    asyncio.create_task(_idle_session_sweeper())

    # Sentinel response: transport.handle_request() already wrote to the ASGI
    # send callable, so Starlette's endpoint wrapper must not send anything
    # else.  Returning this instead of None prevents the "NoneType is not
    # callable" TypeError.
    class _AlreadySent:
        async def __call__(self, scope, receive, send):
            pass

    _ALREADY_SENT = _AlreadySent()

    async def handle_mcp(request: Request):
        import time as _t

        session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        # Route to existing session if client sent a session ID we recognise.
        if session_id and session_id in _sessions:
            transport = _sessions[session_id]
            _session_last_seen[session_id] = _t.monotonic()
            await transport.handle_request(
                request.scope, request.receive, request._send
            )
            # Clean up terminated sessions (e.g. after DELETE).
            if transport._terminated:
                _drop_session(session_id)
            return _ALREADY_SENT

        # Reject new sessions when the cap is reached so a noisy client cannot
        # exhaust memory / asyncio task slots.
        if len(_sessions) >= _MAX_SESSIONS:
            from starlette.responses import Response as StarletteResponse

            return StarletteResponse(
                f"Server at session capacity (max {_MAX_SESSIONS}); retry later.",
                status_code=503,
                headers={"Retry-After": "30"},
            )

        # New session — generate a unique ID so the transport enforces it on
        # all subsequent requests, preventing cross-session pollution.
        new_id = uuid.uuid4().hex
        transport = StreamableHTTPServerTransport(mcp_session_id=new_id)
        _sessions[new_id] = transport
        _session_last_seen[new_id] = _t.monotonic()

        # streams_ready is set once transport.connect() has initialised its
        # internal memory streams.  We must wait for it before calling
        # handle_request(), which writes to those streams.
        streams_ready: asyncio.Event = asyncio.Event()

        async def _session_runner() -> None:
            try:
                async with transport.connect() as (read_stream, write_stream):
                    streams_ready.set()
                    await server.run(
                        read_stream,
                        write_stream,
                        server.create_initialization_options(),
                    )
            except asyncio.CancelledError:
                pass
            finally:
                _sessions.pop(new_id, None)
                _session_tasks.pop(new_id, None)
                _session_last_seen.pop(new_id, None)

        task = asyncio.create_task(_session_runner())
        _session_tasks[new_id] = task

        try:
            # Wait up to 10 s for the transport to be ready.
            await asyncio.wait_for(streams_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _drop_session(new_id)
            from starlette.responses import Response as StarletteResponse

            return StarletteResponse("Session setup timed out", status_code=500)

        try:
            await transport.handle_request(
                request.scope, request.receive, request._send
            )
        except Exception:
            task.cancel()
            raise
        return _ALREADY_SENT

    middleware = []
    auth_mw = _make_auth_middleware()
    if auth_mw:
        middleware.append(auth_mw)
    rate_mw = _make_rate_limit_middleware()
    if rate_mw:
        middleware.append(rate_mw)

    # Phase 6: optional /runtime/* live-ingest routes (off by default).
    from .org.http_routes import make_org_routes
    from .runtime.http_routes import make_runtime_routes

    runtime_routes = make_runtime_routes()
    org_routes = make_org_routes()

    starlette_app = Starlette(
        routes=[
            Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST", "DELETE"]),
            *runtime_routes,
            *org_routes,
        ],
        middleware=middleware,
    )

    print(
        f"jcodemunch-mcp {__version__} by jgravelle · streamable-http server at http://{host}:{port}/mcp",
        file=sys.stderr,
    )
    if not os.environ.get("JCODEMUNCH_HTTP_TOKEN") and host not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        print(
            f"WARNING: streamable-http bound to non-loopback host {host!r} without "
            f"JCODEMUNCH_HTTP_TOKEN — anyone on the network can drive this MCP server. "
            f"Set JCODEMUNCH_HTTP_TOKEN to require bearer auth.",
            file=sys.stderr,
        )
    logger.info(
        "startup version=%s transport=streamable-http host=%s port=%d storage=%s",
        __version__,
        host,
        port,
        os.path.expanduser(os.environ.get("CODE_INDEX_PATH", "~/.code-index/")),
    )
    # Feature 10: Restore session state on startup
    _restore_session_state()
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


def _resolve_log_config(args) -> "tuple[str, Optional[str]]":
    """Resolve (level_name, log_file) with precedence: an explicit CLI flag, then
    the env var (JCODEMUNCH_LOG_LEVEL / JCODEMUNCH_LOG_FILE), then the persisted
    config key (log_level / log_file), then the hardcoded default.

    The config fallback lets `config set log_file <path>` drive logging without
    an env-block or MCP-config edit (e.g. when the jMunch Console enables it),
    while an explicit env var or CLI flag from the launching client still wins.
    Additive: with no env/CLI/config set, this resolves to WARNING + stderr,
    exactly as before."""
    from . import config as _cfg

    level_name = (
        getattr(args, "log_level", None)
        or os.environ.get("JCODEMUNCH_LOG_LEVEL")
        or _cfg.get("log_level", "WARNING")
        or "WARNING"
    )
    log_file = (
        getattr(args, "log_file", None)
        or os.environ.get("JCODEMUNCH_LOG_FILE")
        or _cfg.get("log_file", None)
    )
    return str(level_name).upper(), log_file


def _setup_logging(args) -> None:
    """Configure logging from CLI args / env / config (see _resolve_log_config)."""
    level_name, log_file = _resolve_log_config(args)
    log_level = getattr(logging, level_name, logging.WARNING)
    handlers: list[logging.Handler] = []
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    extra_ext = os.environ.get("JCODEMUNCH_EXTRA_EXTENSIONS", "")
    if extra_ext:
        logging.getLogger(__name__).info("JCODEMUNCH_EXTRA_EXTENSIONS: %s", extra_ext)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add logging args shared by all subcommands."""
    # Defaults are None so _resolve_log_config can apply the full precedence
    # chain (CLI flag > env var > log_level/log_file config key > default).
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level. Precedence: this flag, then JCODEMUNCH_LOG_LEVEL, then the log_level config key, then WARNING.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file path. Precedence: this flag, then JCODEMUNCH_LOG_FILE, then the log_file config key, then stderr.",
    )


def _generate_claude_md_snippet(missing_only: bool = False) -> str:
    """Return the recommended CLAUDE.md prompt-policy snippet.

    When *missing_only* is True, reads ~/.claude/CLAUDE.md and returns only
    the tools not yet mentioned in it (as a minimal addendum block).
    Returns an empty string when the file is already fully up to date.
    """
    all_tools = list(_CANONICAL_TOOL_NAMES)

    if missing_only:
        claude_md = Path.home() / ".claude" / "CLAUDE.md"
        if claude_md.exists():
            content = claude_md.read_text(encoding="utf-8", errors="replace")
            missing = [t for t in all_tools if t not in content]
            if not missing:
                return ""
            tool_lines = "\n".join(f"- {t}" for t in missing)
            return (
                f"<!-- jcodemunch-mcp: add these new tools to your existing snippet -->\n"
                f"{tool_lines}\n"
            )
        # Fall through to full generation if CLAUDE.md doesn't exist yet

    # Group tools by category for readability (single source: module constant).
    categories = _SNIPPET_TOOL_CATEGORIES
    from . import __version__ as _ver

    lines = [
        f"## jcodemunch-mcp (v{_ver})",
        "",
        "Use jcodemunch-mcp tools instead of Grep/Read/Glob for any indexed repository.",
        "",
        "### Quick start",
        "1. `list_repos` — check if the project is indexed.",
        "   If not: `index_folder` (local) or `index_repo` (GitHub URL).",
        "2. `search_symbols` — find functions/classes by name or description.",
        "3. `get_context_bundle` — symbol source + imports in one call.",
        "4. `search_text` — full-text/regex search for literals and comments.",
        "",
        "### All tools",
    ]
    for cat, tools in categories:
        lines.append(f"**{cat}:** " + ", ".join(f"`{t}`" for t in tools))
    lines.append("")
    lines.append("Never fall back to Grep, Read, or Glob for indexed repos.")
    lines.append("")
    return "\n".join(lines)


def _run_claude_md(generate: bool = False, fmt: str = "full") -> None:
    """Output the recommended CLAUDE.md snippet for the current tool set."""
    missing_only = fmt == "append"
    snippet = _generate_claude_md_snippet(missing_only=missing_only)
    if missing_only and not snippet:
        import sys as _sys

        print(
            "CLAUDE.md is already up to date — no new tools to add.", file=_sys.stderr
        )
        return
    print(snippet, end="")


def _run_config(check: bool = False, init: bool = False, upgrade: bool = False) -> None:
    """Print the current effective configuration to stdout, or initialize config file."""
    from . import __version__
    from . import config as _cfg

    # Project-aware getter wrapper (issue #300 follow-up, surfaced by @slazarov).
    # If cwd has a .jcodemunch.jsonc, load it and route _cfg.get() through a
    # shim that injects repo=cwd when callers don't pass one explicitly. Without
    # this, `config --check` reports the project file as valid but the printed
    # config values still come from _GLOBAL_CONFIG alone, so any project-level
    # override is silently invisible in diagnostic output.
    _project_repo_key: Optional[str] = None
    _project_loaded_keys: set = set()
    _project_config_path_for_display = Path.cwd() / ".jcodemunch.jsonc"
    if _project_config_path_for_display.is_file():
        try:
            _cfg.load_project_config(str(Path.cwd()))
            _project_repo_key = str(Path.cwd().resolve())
            try:
                _pc_content = _project_config_path_for_display.read_text(
                    encoding="utf-8"
                )
                import json as _json_pc

                _project_loaded_keys = set(
                    _json_pc.loads(_cfg._strip_jsonc(_pc_content)).keys()
                )
            except Exception:
                _project_loaded_keys = set()
        except Exception:
            _project_repo_key = None
            _project_loaded_keys = set()

    class _ProjectAwareCfg:
        """Routes get() through the project-merged config when cwd has one."""

        def __init__(self, module, repo):
            self.__dict__["_mod"] = module
            self.__dict__["_repo"] = repo

        def get(self, key, default=None, repo=None):
            if repo is None:
                repo = self._repo
            return self._mod.get(key, default, repo=repo)

        def __getattr__(self, name):
            return getattr(self._mod, name)

    _cfg = _ProjectAwareCfg(_cfg, _project_repo_key)

    # Handle --upgrade
    if upgrade:
        storage_path = os.environ.get(
            "CODE_INDEX_PATH", str(Path.home() / ".code-index")
        )
        config_path = Path(storage_path) / "config.jsonc"

        if not config_path.exists():
            print(f"No config file found at: {config_path}")
            print("Run `config --init` first to create one.")
            return

        added, warnings = _cfg.upgrade_config(config_path)
        if not added:
            print(f"Config is already up to date (version bumped to {__version__}).")
        else:
            print(
                f"Upgraded config to {__version__}. Added {len(added)} missing key(s):"
            )
            for key in added:
                print(f"  + {key}")
        for w in warnings:
            print(f"  warning: {w}")
        return

    # Handle --init
    if init:
        storage_path = os.environ.get(
            "CODE_INDEX_PATH", str(Path.home() / ".code-index")
        )
        config_path = Path(storage_path) / "config.jsonc"

        if config_path.exists():
            print(f"Config file already exists: {config_path}")
            print(
                "Refusing to overwrite. Remove it first or use --check to validate it."
            )
            return

        config_path.parent.mkdir(parents=True, exist_ok=True)
        template = _cfg.generate_template()
        config_path.write_text(template, encoding="utf-8")
        print(f"Created config template: {config_path}")
        print("Edit it to customize jcodemunch-mcp settings.")
        return

    # Load config to get effective values
    _cfg.load_config()

    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    enc = getattr(sys.stdout, "encoding", "ascii") or "ascii"

    def _safe(s, fallback):
        try:
            s.encode(enc)
            return s
        except (UnicodeEncodeError, LookupError):
            return fallback

    CHECK = _safe("✓", "OK")
    CROSS = _safe("✗", "!!")
    WARN = _safe("!", "!")

    def dim(s):
        return f"\033[2m{s}\033[0m" if tty else s

    def bold(s):
        return f"\033[1m{s}\033[0m" if tty else s

    def green(s):
        return f"\033[32m{s}\033[0m" if tty else s

    def yellow(s):
        return f"\033[33m{s}\033[0m" if tty else s

    def red(s):
        return f"\033[31m{s}\033[0m" if tty else s

    COL = 36

    def row(name, value, source="default"):
        tag = dim(f" [{source}]") if source != "default" else dim(" (default)")
        print(f"  {name:<{COL}} {value}{tag}")

    def env(var, default=""):
        val = os.environ.get(var)
        return (val if val is not None else default), (val is None)

    def section(title):
        print(f"\n{bold(title)}")

    def cfg_row(name, key, default, source=None, fmt=None):
        """Display a config value with source indicator."""
        val = _cfg.get(key, default)
        if fmt:
            val = fmt(val)
        effective_source = source or "default"
        print(f"  {name:<{COL}} {val}{dim(f' [{effective_source}]')}")

    print(bold(f"jcodemunch-mcp {__version__} — configuration"))

    # ── Config File ───────────────────────────────────────────────────────
    section("Config File")
    storage_path = os.environ.get("CODE_INDEX_PATH", str(Path.home() / ".code-index"))
    config_path = Path(storage_path) / "config.jsonc"
    if config_path.exists():
        print(f"  {green(CHECK)} config.jsonc found: {config_path}")
    else:
        print(f"  {yellow(WARN)} config.jsonc not found: {config_path}")
        print(
            f"  {dim('  Using defaults + env var fallbacks. Run `config --init` to create a config file.')}"
        )
    # Project-level .jcodemunch.jsonc visibility (jdoc #300 follow-up).
    if _project_repo_key is not None:
        print(
            f"  {green(CHECK)} .jcodemunch.jsonc loaded from cwd: {_project_config_path_for_display} "
            f"{dim(f'({len(_project_loaded_keys)} key(s) override global)')}"
        )
    elif _project_config_path_for_display.is_file():
        print(
            f"  {yellow(WARN)} .jcodemunch.jsonc present but failed to load: "
            f"{_project_config_path_for_display}"
        )

    # ── Indexing ──────────────────────────────────────────────────────────
    section("Indexing")
    # Detect source for each config key
    # Check the actual config file content (if exists) to determine if a key was
    # explicitly set in config vs defaulted
    _loaded_keys: set = set()
    if config_path.exists():
        try:
            content = config_path.read_text(encoding="utf-8")
            stripped = _cfg._strip_jsonc(content)
            import json as _json

            _loaded_keys = set(_json.loads(stripped).keys())
        except Exception:
            pass

    def _detect_source(key, default):
        if key in _project_loaded_keys:
            return "project"
        if key in _loaded_keys:
            return "config"
        env_var = next((e for e, c in _cfg.ENV_VAR_MAPPING.items() if c == key), None)
        if env_var and os.environ.get(env_var) is not None:
            return "env"
        return "default"

    def _fmt_list(v):
        if isinstance(v, list):
            return f"[{len(v)} items]" if len(v) > 3 else str(v)
        return str(v)

    row(
        "max_folder_files",
        _cfg.get("max_folder_files", 2000),
        _detect_source("max_folder_files", 2000),
    )
    row(
        "max_index_files",
        _cfg.get("max_index_files", 10000),
        _detect_source("max_index_files", 10000),
    )
    row(
        "staleness_days",
        _cfg.get("staleness_days", 7),
        _detect_source("staleness_days", 7),
    )
    row("max_results", _cfg.get("max_results", 500), _detect_source("max_results", 500))
    patterns = _cfg.get("extra_ignore_patterns", [])
    row(
        "extra_ignore_patterns",
        _fmt_list(patterns) if patterns else dim("(none)"),
        _detect_source("extra_ignore_patterns", []),
    )
    exts = _cfg.get("extra_extensions", {})
    row(
        "extra_extensions",
        _fmt_list(exts) if exts else dim("(none)"),
        _detect_source("extra_extensions", {}),
    )
    row(
        "context_providers",
        str(_cfg.get("context_providers", True)).lower(),
        _detect_source("context_providers", True),
    )
    path_map_val = _cfg.get("path_map", "")
    row(
        "path_map",
        path_map_val if path_map_val else dim("(none)"),
        _detect_source("path_map", ""),
    )

    # ── Meta Response Control ─────────────────────────────────────────────
    section("Meta Response Control")
    meta_fields = _cfg.get("meta_fields")
    if meta_fields is None:
        row("meta_fields", dim("(all fields)"), "config")
    elif meta_fields == []:
        row("meta_fields", dim("(none)"), _detect_source("meta_fields", []))
    else:
        row("meta_fields", _fmt_list(meta_fields), _detect_source("meta_fields", None))

    # ── Languages ─────────────────────────────────────────────────────────
    section("Languages")
    languages = _cfg.get("languages")
    if languages is None:
        row("languages", dim("(all languages)"), "default")
    else:
        row("languages", _fmt_list(languages), _detect_source("languages", None))

    # ── Compact Schemas ──────────────────────────────────────────────────
    section("Compact Schemas")
    compact = _cfg.get("compact_schemas", False)
    row(
        "compact_schemas",
        green("enabled") if compact else dim("disabled"),
        _detect_source("compact_schemas", False),
    )

    # ── Disabled Tools ────────────────────────────────────────────────────
    section("Disabled Tools")
    disabled = _cfg.get("disabled_tools", [])
    row(
        "disabled_tools",
        _fmt_list(disabled) if disabled else dim("(none)"),
        _detect_source("disabled_tools", []),
    )

    # ── Descriptions ──────────────────────────────────────────────────────
    section("Descriptions")
    descs = _cfg.get("descriptions", {})
    row(
        "descriptions",
        _fmt_list(descs) if descs else dim("(none)"),
        _detect_source("descriptions", {}),
    )

    # ── AI Summarizer ─────────────────────────────────────────────────────
    section("AI Summarizer")
    use_ai_raw, use_ai_d = env("JCODEMUNCH_USE_AI_SUMMARIES", "true")
    use_ai = use_ai_raw.lower() not in ("false", "0", "no", "off")
    row(
        "use_ai_summaries",
        str(use_ai).lower(),
        "env" if not use_ai_d else _detect_source("use_ai_summaries", True),
    )
    provider, provider_d = env("JCODEMUNCH_SUMMARIZER_PROVIDER", "")
    row(
        "summarizer_provider",
        provider if provider else dim("(auto-detect)"),
        "env" if not provider_d else "default",
    )

    # summarizer_model display (surfaced by @slazarov on #300, runtime fix #304).
    # As of v1.108.18, batch_summarize.py threads `repo=` through every
    # _config.get() call, so .jcodemunch.jsonc overrides DO flow to the runtime.
    # The display can now use the project-aware shim value directly.
    _sm_effective = (_cfg.get("summarizer_model", "") or "").strip()
    if _sm_effective:
        row("summarizer_model", _sm_effective, _detect_source("summarizer_model", ""))
    else:
        row("summarizer_model", dim("(provider default)"), "default")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    openai_base = os.environ.get("OPENAI_API_BASE", "")
    provider_name = get_provider_name()

    if not use_ai:
        print(f"  {yellow('AI summaries disabled')} — signature fallback active")
    elif provider_name == "anthropic":
        suffix = (
            "JCODEMUNCH_SUMMARIZER_PROVIDER=anthropic"
            if provider == "anthropic"
            else "ANTHROPIC_API_KEY set"
        )
        print(f"  Active provider:  {green('Anthropic')}  ({suffix})")
        # Runtime: summarizer_model (config; project-aware as of #304) > ANTHROPIC_MODEL env > default
        if _sm_effective:
            row(
                "  ANTHROPIC_MODEL",
                _sm_effective,
                _detect_source("summarizer_model", ""),
            )
        else:
            model, d = env("ANTHROPIC_MODEL", "claude-haiku-*")
            row("  ANTHROPIC_MODEL", model, "env" if not d else "default")
    elif provider_name == "gemini":
        suffix = (
            "JCODEMUNCH_SUMMARIZER_PROVIDER=gemini"
            if provider == "gemini"
            else "GOOGLE_API_KEY set"
        )
        print(f"  Active provider:  {green('Google Gemini')}  ({suffix})")
        if _sm_effective:
            row("  GOOGLE_MODEL", _sm_effective, _detect_source("summarizer_model", ""))
        else:
            model, d = env("GOOGLE_MODEL", "gemini-flash-*")
            row("  GOOGLE_MODEL", model, "env" if not d else "default")
    elif provider_name == "openai":
        base_label = openai_base or "https://api.openai.com/v1"
        suffix = (
            "JCODEMUNCH_SUMMARIZER_PROVIDER=openai"
            if provider == "openai"
            else "OPENAI_API_BASE set"
        )
        print(f"  Active provider:  {green('OpenAI-compatible')}  ({suffix})")
        row("  OPENAI_API_BASE", base_label, "env" if openai_base else "default")
        if _sm_effective:
            row("  OPENAI_MODEL", _sm_effective, _detect_source("summarizer_model", ""))
        else:
            model_default = (
                "gpt-4o-mini"
                if provider == "openai" and not openai_base
                else "qwen3-coder"
            )
            model, d = env("OPENAI_MODEL", model_default)
            row("  OPENAI_MODEL", model, "env" if not d else "default")
        v, d = env("OPENAI_TIMEOUT", "60.0")
        row("  OPENAI_TIMEOUT", v, "env" if not d else "default")
        v, d = env("OPENAI_BATCH_SIZE", "10")
        row("  OPENAI_BATCH_SIZE", v, "env" if not d else "default")
        v, d = env("OPENAI_CONCURRENCY", str(_cfg.get("summarizer_concurrency", 4)))
        row("  OPENAI_CONCURRENCY", v, "env" if not d else "config")
        v, d = env("OPENAI_MAX_TOKENS", "500")
        row("  OPENAI_MAX_TOKENS", v, "env" if not d else "default")
    elif provider_name == "minimax":
        suffix = (
            "JCODEMUNCH_SUMMARIZER_PROVIDER=minimax"
            if provider == "minimax"
            else "MINIMAX_API_KEY set"
        )
        print(f"  Active provider:  {green('MiniMax')}  ({suffix})")
        row("  OPENAI_API_BASE", "https://api.minimax.io/v1", "default")
        row(
            "  OPENAI_MODEL",
            _sm_effective or "minimax-m2.7",
            _detect_source("summarizer_model", "") if _sm_effective else "default",
        )
    elif provider_name == "glm":
        suffix = (
            "JCODEMUNCH_SUMMARIZER_PROVIDER=glm"
            if provider == "glm"
            else "ZHIPUAI_API_KEY set"
        )
        print(f"  Active provider:  {green('GLM-5')}  ({suffix})")
        row("  OPENAI_API_BASE", "https://api.z.ai/api/paas/v4/", "default")
        row(
            "  OPENAI_MODEL",
            _sm_effective or "glm-5",
            _detect_source("summarizer_model", "") if _sm_effective else "default",
        )
    elif provider_name == "openrouter":
        suffix = (
            "JCODEMUNCH_SUMMARIZER_PROVIDER=openrouter"
            if provider == "openrouter"
            else "OPENROUTER_API_KEY set"
        )
        print(f"  Active provider:  {green('OpenRouter')}  ({suffix})")
        row("  OPENAI_API_BASE", "https://openrouter.ai/api/v1", "default")
        row(
            "  OPENAI_MODEL",
            _sm_effective or "meta-llama/llama-3.3-70b-instruct:free",
            _detect_source("summarizer_model", "") if _sm_effective else "default",
        )
    elif provider == "none":
        print(
            f"  Active provider:  {yellow('none')} — explicitly disabled, signature fallback active"
        )
    else:
        print(
            f"  Active provider:  {yellow('none')} — no API key set, signature fallback active"
        )
        print(
            f"  {dim('Set ANTHROPIC_API_KEY, GOOGLE_API_KEY, OPENAI_API_BASE, MINIMAX_API_KEY, ZHIPUAI_API_KEY, or OPENROUTER_API_KEY to enable')}"
        )

    allow_remote = _cfg.get("allow_remote_summarizer", False)
    allow_label = str(allow_remote).lower()
    if not allow_remote and provider_name:
        allow_label += (
            f" {dim('(only affects custom base URLs, not standard API endpoints)')}"
        )
    row(
        "allow_remote_summarizer",
        allow_label,
        _detect_source("allow_remote_summarizer", False),
    )

    # ── Transport ──────────────────────────────────────────────────────────
    section("Transport")
    transport = _cfg.get("transport", "stdio")
    row("transport", transport, _detect_source("transport", "stdio"))
    if transport != "stdio":
        row("host", _cfg.get("host", "127.0.0.1"), _detect_source("host", "127.0.0.1"))
        row("port", _cfg.get("port", 8901), _detect_source("port", 8901))
        token = os.environ.get("JCODEMUNCH_HTTP_TOKEN", "")
        try:
            from . import credentials as _creds

            _kr_source = _creds.get_keyring_source_for("JCODEMUNCH_HTTP_TOKEN")
        except Exception:
            _kr_source = None
        _kr_label = f"keyring:{_kr_source}" if _kr_source else "env"
        row(
            "JCODEMUNCH_HTTP_TOKEN",
            green("set") if token else yellow("not set"),
            _kr_label,
        )
        rate = _cfg.get("rate_limit", 0)
        rate_label = f"{rate}/min per IP" if rate != 0 else "disabled"
        row("rate_limit", rate_label, _detect_source("rate_limit", 0))
    else:
        print(f"  {dim('stdio mode — HTTP transport vars ignored')}")

    # ── Watcher ───────────────────────────────────────────────────────────
    section("Watcher")
    row("watch", str(_cfg.get("watch", False)).lower(), _detect_source("watch", False))
    row(
        "watch_debounce_ms",
        _cfg.get("watch_debounce_ms", 2000),
        _detect_source("watch_debounce_ms", 2000),
    )
    row(
        "freshness_mode",
        _cfg.get("freshness_mode", "relaxed"),
        _detect_source("freshness_mode", "relaxed"),
    )
    row(
        "claude_poll_interval",
        _cfg.get("claude_poll_interval", 5.0),
        _detect_source("claude_poll_interval", 5.0),
    )

    # ── Logging ──────────────────────────────────────────────────────────
    section("Logging")
    row(
        "log_level",
        _cfg.get("log_level", "WARNING"),
        _detect_source("log_level", "WARNING"),
    )
    log_file = _cfg.get("log_file")
    row(
        "log_file",
        log_file if log_file else dim("(stderr)"),
        _detect_source("log_file", None),
    )

    # ── Privacy & Telemetry ───────────────────────────────────────────────
    section("Privacy & Telemetry")
    row(
        "redact_source_root",
        str(_cfg.get("redact_source_root", False)).lower(),
        _detect_source("redact_source_root", False),
    )
    stats_int = _cfg.get("stats_file_interval", 3)
    row(
        "stats_file_interval",
        "disabled" if stats_int == 0 else f"every {stats_int} calls",
        _detect_source("stats_file_interval", 3),
    )
    share = _cfg.get("share_savings", True)
    row(
        "share_savings",
        green("enabled") if share else yellow("disabled"),
        _detect_source("share_savings", True),
    )
    row(
        "summarizer_concurrency",
        _cfg.get("summarizer_concurrency", 4),
        _detect_source("summarizer_concurrency", 4),
    )

    # ── Keyring resolution (P1.3) ─────────────────────────────────────────
    # Surfaces which credential env vars were resolved from the system keyring
    # at startup. Helps an operator confirm the chokepoint is firing without
    # having to inspect the actual secret value.
    try:
        from . import credentials as _creds

        _resolved = [
            (var, _creds.get_keyring_source_for(var))
            for var in _creds.list_recognised_env_vars()
            if _creds.get_keyring_source_for(var) is not None
        ]
        if _resolved:
            section("Keyring resolution")
            for var, entry in _resolved:
                row(var, green("resolved"), f"keyring:{entry}")
    except Exception:
        pass  # keyring not installed, env vars not touched — nothing to show

    # ── --check ───────────────────────────────────────────────────────────
    if check:
        section("Checks")
        issues: list[str] = []
        # Sandbox/host-visibility-limited probes that could NOT be confirmed
        # either way from inside this process. Distinct from `issues`: a
        # restricted agent shell that cannot prove host writability must not be
        # reported as a confirmed configuration failure (issue #335).
        host_confirmation: list[str] = []

        # Validate config.jsonc
        config_issues = _cfg.validate_config(str(config_path))
        if config_issues:
            for issue in config_issues:
                print(f"  {red(CROSS)} config.jsonc: {issue}")
            issues.append("config")
        else:
            print(f"  {green(CHECK)} config.jsonc valid: {config_path}")

        # Probe cwd for project-level .jcodemunch.jsonc and validate if found.
        # Without this, users editing project config see no signal that the
        # file is being parsed at all (issue #300).
        project_config_path = Path.cwd() / ".jcodemunch.jsonc"
        if project_config_path.is_file():
            project_issues = _cfg.validate_config(str(project_config_path))
            if project_issues:
                for issue in project_issues:
                    print(f"  {red(CROSS)} .jcodemunch.jsonc: {issue}")
                issues.append("project_config")
            else:
                print(
                    f"  {green(CHECK)} .jcodemunch.jsonc valid: {project_config_path}"
                )

        # Storage writable?
        storage = Path(storage_path)
        try:
            storage.mkdir(parents=True, exist_ok=True)
            probe = storage / ".jcm_probe"
            probe.write_text("ok")
            probe.unlink()
            print(f"  {green(CHECK)} index storage writable: {storage}")
        except PermissionError as e:
            # In a sandboxed/restricted agent shell, EPERM/EACCES means "this
            # process cannot prove host writability", NOT "the host index
            # storage is actually unwritable". Don't present an indeterminate
            # sandbox probe as a confirmed failure (issue #335) — flag it for
            # host confirmation and tell the operator to rerun unsandboxed.
            if e.errno in {errno.EPERM, errno.EACCES}:
                print(
                    f"  {yellow(WARN)} index storage writability needs host confirmation: "
                    f"{storage} — {e}"
                )
                host_confirmation.append("storage")
            else:
                print(f"  {red(CROSS)} index storage not writable: {storage} — {e}")
                issues.append("storage")
        except Exception as e:
            print(f"  {red(CROSS)} index storage not writable: {storage} — {e}")
            issues.append("storage")

        # AI provider package installed?
        if use_ai:
            if provider_name == "anthropic":
                try:
                    import anthropic as _a

                    print(
                        f"  {green(CHECK)} anthropic package installed (v{_a.__version__})"
                    )
                except ImportError:
                    print(
                        f'  {red(CROSS)} anthropic not installed — run: pip install "jcodemunch-mcp[anthropic]"'
                    )
                    issues.append("anthropic")
            elif provider_name == "gemini":
                try:
                    import google.generativeai  # noqa: F401

                    print(f"  {green(CHECK)} google-generativeai package installed")
                except ImportError:
                    print(
                        f'  {red(CROSS)} google-generativeai not installed — run: pip install "jcodemunch-mcp[gemini]"'
                    )
                    issues.append("gemini")
            elif provider_name in {"openai", "minimax", "glm"}:
                try:
                    import httpx  # noqa: F401

                    print(
                        f"  {green(CHECK)} httpx available for OpenAI-compatible requests"
                    )
                except ImportError:
                    print(
                        f"  {red(CROSS)} httpx not installed (required for OpenAI-compatible summarizer)"
                    )
                    issues.append("httpx")
            else:
                print(
                    f"  {yellow(WARN)} no AI provider configured — signature fallback will be used"
                )

        # HTTP transport packages installed?
        if transport != "stdio":
            missing = [
                pkg for pkg in ("uvicorn", "starlette", "anyio") if not _can_import(pkg)
            ]
            if missing:
                print(
                    f'  {red(CROSS)} HTTP packages missing: {", ".join(missing)} — run: pip install "jcodemunch-mcp[http]"'
                )
                issues.append("http")
            else:
                print(
                    f"  {green(CHECK)} HTTP transport packages installed (uvicorn, starlette, anyio)"
                )

        # ── CLAUDE.md drift check ────────────────────────────────────────────
        section("CLAUDE.md check")
        claude_md_path = Path.home() / ".claude" / "CLAUDE.md"
        canonical_tools = list(_CANONICAL_TOOL_NAMES)
        if claude_md_path.exists():
            try:
                cm_content = claude_md_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                # Check for the one-line form that delegates to jcodemunch_guide
                # (Issue #271: "Call the jcodemunch_guide tool and strictly follow its instructions.")
                if "jcodemunch_guide" in cm_content and "strictly follow" in cm_content:
                    print(
                        f"  {green(CHECK)} CLAUDE.md uses one-line form (jcodemunch_guide) — skipping tool-by-tool check"
                    )
                else:
                    missing_in_cm = [t for t in canonical_tools if t not in cm_content]
                    if missing_in_cm:
                        _wrapped = _wrap_names(missing_in_cm)
                        print(
                            f"  {yellow(WARN)} {len(missing_in_cm)} tool(s) not mentioned in CLAUDE.md:"
                        )
                        for _line in _wrapped:
                            print(f"       {dim(_line)}")
                        print(
                            f"  {dim('  Run: jcodemunch-mcp claude-md --generate  (or --format=append for delta only)')}"
                        )
                        issues.append("claude_md")
                    else:
                        print(
                            f"  {green(CHECK)} All {len(canonical_tools)} tools mentioned in CLAUDE.md"
                        )
            except Exception as _e:
                print(f"  {yellow(WARN)} Could not read CLAUDE.md: {_e}")
        else:
            print(f"  {yellow(WARN)} CLAUDE.md not found: {claude_md_path}")
            print(
                f"  {dim('  Run: jcodemunch-mcp claude-md --generate > /path/to/CLAUDE.md')}"
            )

        # ── Hook check ─────────────────────────────────────────────────────────
        section("Hooks check")
        _settings_path = Path.home() / ".claude" / "settings.json"
        _expected_hooks = {
            "hook-pretooluse": ("PreToolUse", "Read"),
            "hook-posttooluse": ("PostToolUse", "Edit|Write"),
            "hook-precompact": ("PreCompact", ""),
            "hook-taskcomplete": ("TaskCompleted", ""),
            "hook-subagent-start": ("SubagentStart", ""),
        }
        if _settings_path.exists():
            try:
                _settings = json.loads(_settings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _settings = {}
            _installed_hooks = _settings.get("hooks", {})
            _found_any = False
            for _hook_cmd, (_event, _matcher) in _expected_hooks.items():
                _marker = f"jcodemunch-mcp {_hook_cmd}"
                _present = False
                for _rule in _installed_hooks.get(_event, []):
                    for _h in _rule.get("hooks", []):
                        if _marker in _h.get("command", ""):
                            _present = True
                            break
                if _present:
                    _label = f"{_event}({_matcher})" if _matcher else _event
                    print(f"  {green(CHECK)} {_hook_cmd} installed [{_label}]")
                    _found_any = True
                else:
                    print(f"  {dim(f'  {_hook_cmd} not installed')}")
            if not _found_any:
                print(f"  {dim('  Run: jcodemunch-mcp init --hooks')}")
            # Warn about legacy shell scripts
            _hooks_dir = Path.home() / ".claude" / "hooks"
            if _hooks_dir.exists():
                _legacy = (
                    list(_hooks_dir.glob("jcodemunch_read_guard.*"))
                    + list(_hooks_dir.glob("jcodemunch_edit_guard.*"))
                    + list(_hooks_dir.glob("jcodemunch_index_hook.*"))
                )
                if _legacy:
                    print(
                        f"  {yellow(WARN)} Legacy shell scripts detected (replaced by Python hooks):"
                    )
                    for _script in sorted(_legacy):
                        print(f"       {dim(_script.name)}")
                    print(
                        f"       {dim('These can be removed. Run: jcodemunch-mcp init --hooks')}"
                    )
        else:
            print(
                f"  {dim('(~/.claude/settings.json not found — hooks not installed)')}"
            )
            print(f"  {dim('  Run: jcodemunch-mcp init --hooks')}")

        print()
        if issues:
            print(yellow(f"  {len(issues)} issue(s) found — see above."))
            sys.exit(1)
        elif host_confirmation:
            # No confirmed failures, but at least one probe could only be
            # answered by the host (sandbox-limited). Exit 0 so an agent client
            # does not mistake a healthy install for a broken one, but tell the
            # operator to rerun outside the sandbox before acting on it (#335).
            print(
                yellow(
                    f"  {len(host_confirmation)} check(s) need host confirmation — see above."
                )
            )
            print(
                dim(
                    "  Rerun outside a sandbox or restricted shell before repairing"
                    " or reporting drift."
                )
            )
        else:
            print(green("  All checks passed."))
    print()


def _wrap_names(names: list[str], width: int = 72) -> list[str]:
    """Wrap a flat list of names into lines no longer than *width* chars."""
    lines: list[str] = []
    current = ""
    for name in names:
        piece = (", " if current else "") + name
        if current and len(current) + len(piece) > width:
            lines.append(current)
            current = name
        else:
            current += piece
    if current:
        lines.append(current)
    return lines


def _can_import(module: str) -> bool:
    """Return True if module is importable without side effects."""
    import importlib.util

    return importlib.util.find_spec(module) is not None


def main(argv: Optional[list[str]] = None):
    """Main entry point."""
    from .security import verify_package_integrity

    verify_package_integrity()

    parser = argparse.ArgumentParser(
        prog="jcodemunch-mcp",
        description="jCodeMunch MCP server and tools.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")

    # --- serve (default when no subcommand given) ---
    serve_parser = subparsers.add_parser("serve", help="Run the MCP server (default)")
    serve_parser.add_argument(
        "--transport",
        default=os.environ.get("JCODEMUNCH_TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
        help="Transport mode: stdio (default), sse, or streamable-http (also via JCODEMUNCH_TRANSPORT env var)",
    )
    serve_parser.add_argument(
        "--host",
        default=os.environ.get("JCODEMUNCH_HOST", "127.0.0.1"),
        help="Host to bind to in HTTP transport mode (also via JCODEMUNCH_HOST env var, default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("JCODEMUNCH_PORT", "8901")),
        help="Port to listen on in HTTP transport mode (also via JCODEMUNCH_PORT env var, default: 8901)",
    )
    _add_common_args(serve_parser)

    # --- Watcher options for serve ---
    serve_parser.add_argument(
        "--watcher",
        nargs="?",
        const="true",
        default=None,
        metavar="BOOL",
        help="Enable background file watcher alongside the server. "
        "Use --watcher or --watcher=true to enable, --watcher=false to disable.",
    )
    serve_parser.add_argument(
        "--watcher-path",
        nargs="*",
        default=None,
        metavar="PATH",
        help="Folder(s) to watch (default: current working directory)",
    )
    serve_parser.add_argument(
        "--watcher-debounce",
        type=int,
        default=None,
        metavar="MS",
        help="Watcher debounce interval in ms (default: from config, also via JCODEMUNCH_WATCH_DEBOUNCE_MS)",
    )
    serve_parser.add_argument(
        "--watcher-idle-timeout",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Auto-stop watcher after N minutes with no re-indexing (default: disabled)",
    )
    serve_parser.add_argument(
        "--watcher-no-ai-summaries",
        action="store_true",
        help="Disable AI-generated summaries for watcher re-indexing",
    )
    serve_parser.add_argument(
        "--watcher-extra-ignore",
        nargs="*",
        help="Additional gitignore-style patterns to exclude from watching",
    )
    serve_parser.add_argument(
        "--watcher-follow-symlinks",
        action="store_true",
        help="Include symlinked files in watcher indexing",
    )
    serve_parser.add_argument(
        "--watcher-log",
        nargs="?",
        const="auto",
        default=None,
        metavar="PATH",
        help="Log watcher output to file instead of stderr. "
        "Use --watcher-log for auto temp file, or --watcher-log=<path> for a specific file.",
    )
    serve_parser.add_argument(
        "--freshness-mode",
        default=None,
        choices=["relaxed", "strict"],
        help="Freshness mode: 'relaxed' (default) or 'strict' (block queries until watcher reindex finishes)",
    )

    # --- watch ---
    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch folders for changes and auto-reindex",
    )
    watch_parser.add_argument(
        "paths",
        nargs="+",
        help="One or more folder paths to watch",
    )
    watch_parser.add_argument(
        "--debounce",
        type=int,
        default=None,
        metavar="MS",
        help="Debounce interval in ms (default: from config, also via JCODEMUNCH_WATCH_DEBOUNCE_MS)",
    )
    watch_parser.add_argument(
        "--no-ai-summaries",
        action="store_true",
        help="Disable AI-generated summaries during re-indexing",
    )
    watch_parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Include symlinked files in indexing",
    )
    watch_parser.add_argument(
        "--extra-ignore",
        nargs="*",
        help="Additional gitignore-style patterns to exclude",
    )
    watch_parser.add_argument(
        "--idle-timeout",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Auto-shutdown after N minutes with no re-indexing (default: disabled)",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="Index all paths once (incremental) and exit immediately — no file watching",
    )
    _add_common_args(watch_parser)

    # --- config ---
    config_parser = subparsers.add_parser(
        "config",
        help="Show current effective configuration",
    )
    config_parser.add_argument(
        "--check",
        action="store_true",
        help="Also verify prerequisites (storage writable, AI packages installed, HTTP packages present)",
    )
    config_parser.add_argument(
        "--init",
        action="store_true",
        help="Generate a template config.jsonc file in CODE_INDEX_PATH",
    )
    config_parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Add missing keys from the current template to an existing config.jsonc, preserving user values",
    )
    config_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the effective configuration as structured JSON (key/type/value/default/source) for tooling",
    )
    config_parser.add_argument(
        "action",
        nargs="?",
        choices=["set", "unset"],
        help="set <key> <value> to write a config key, or unset <key> to clear it (default applies)",
    )
    config_parser.add_argument("key", nargs="?", help="config key for set/unset")
    config_parser.add_argument(
        "value",
        nargs="?",
        help='value for set: JSON (true, 7, ["a"], {"k":1}) or a bare string',
    )

    # --- list-repos ---
    list_repos_parser = subparsers.add_parser(
        "list-repos",
        help="List indexed repositories with counts, freshness, and watcher state",
    )
    list_repos_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON (repo_id/counts/languages/indexed_at/freshness/watcher_state/lock_holder)",
    )

    # --- delete-index (CLI command for index deletion) ---
    delete_index_parser = subparsers.add_parser(
        "delete-index",
        help="Delete a repository's index and cached data",
    )
    delete_index_parser.add_argument(
        "repo",
        help="Repository identifier (owner/repo or repo name, as shown by list-repos)",
    )
    delete_index_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured {success, repo, message|error} JSON result",
    )

    # --- org-report / org-rollup (team SKU) ---
    org_report_parser = subparsers.add_parser(
        "org-report",
        help="Record this seat's token savings under its org (JCODEMUNCH_ORG_ID)",
    )
    org_report_parser.add_argument(
        "--org", help="Org identifier (overrides JCODEMUNCH_ORG_ID)"
    )
    org_report_parser.add_argument(
        "--seat", help="Seat identifier (default: JCODEMUNCH_CLIENT_ID or hostname)"
    )
    org_report_parser.add_argument(
        "--endpoint",
        help="Org host URL to POST to (overrides JCODEMUNCH_ORG_ENDPOINT); omit to record locally",
    )
    org_report_parser.add_argument(
        "--model",
        default="opus",
        choices=["sonnet", "opus", "haiku"],
        help="Rate for the $ figure",
    )
    org_report_parser.add_argument("--json", action="store_true", help="Emit JSON")

    org_rollup_parser = subparsers.add_parser(
        "org-rollup",
        help="Aggregate token savings across all seats in an org",
    )
    org_rollup_parser.add_argument(
        "--org", help="Org identifier (overrides JCODEMUNCH_ORG_ID)"
    )
    org_rollup_parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON (seats[] + totals)"
    )

    license_parser = subparsers.add_parser(
        "license",
        help="Check jCodeMunch license status (gates the org-rollup team feature)",
    )
    license_parser.add_argument(
        "--key", help="Validate this key (else uses JCODEMUNCH_LICENSE_KEY / config)"
    )
    license_parser.add_argument("--json", action="store_true", help="Emit JSON status")

    # --- claude-md ---
    claude_md_parser = subparsers.add_parser(
        "claude-md",
        help="Generate a CLAUDE.md prompt-policy snippet for the current tool set",
    )
    claude_md_parser.add_argument(
        "--generate",
        action="store_true",
        help="Output the recommended CLAUDE.md snippet to stdout",
    )
    claude_md_parser.add_argument(
        "--format",
        choices=["full", "append"],
        default="full",
        dest="fmt",
        help="'full' (default) — complete snippet; 'append' — only tools not yet in your CLAUDE.md",
    )

    # --- index-file ---
    # --- index (full folder/repo index) ---
    index_parser = subparsers.add_parser(
        "index",
        help="Index a local folder or GitHub repo (default: current directory)",
    )
    index_parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Local path or owner/repo (default: current directory)",
    )
    index_parser.add_argument(
        "--no-ai-summaries",
        action="store_true",
        help="Disable AI-generated summaries",
    )
    index_parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Include symlinked files in indexing",
    )
    index_parser.add_argument(
        "--extra-ignore",
        nargs="*",
        help="Additional gitignore-style patterns to exclude",
    )
    index_parser.add_argument(
        "--paths-from",
        metavar="FILE",
        help=(
            "Read explicit paths to index (one per line) from FILE. Use '-' for "
            "stdin. When set, the directory walk is skipped — only the listed "
            "paths are indexed. Entries may be absolute or relative to the "
            "target. Pipe-friendly with git / find / fd / rg. Lines starting "
            "with `#` are comments."
        ),
    )
    _add_common_args(index_parser)

    # --- index-file ---
    index_file_parser = subparsers.add_parser(
        "index-file",
        help="Re-index a single file within an existing indexed folder",
    )
    index_file_parser.add_argument(
        "path",
        help="Absolute path to the file to index",
    )
    index_file_parser.add_argument(
        "--no-ai-summaries",
        action="store_true",
        help="Disable AI-generated summaries for this file",
    )
    _add_common_args(index_file_parser)

    # --- import-trace (Phases 1 + 4 + 5: OTel + SQL log + stack log ingest) ---
    import_trace_parser = subparsers.add_parser(
        "import-trace",
        help="Ingest a runtime trace file (OTel / SQL log / stack log) into the runtime_* tables",
    )
    import_trace_parser.add_argument(
        "--otel",
        dest="otel_path",
        metavar="PATH",
        help="Path to an OTel JSON, JSON-Lines, or .gz trace file",
    )
    import_trace_parser.add_argument(
        "--sql-log",
        dest="sql_log_path",
        metavar="PATH",
        help="Path to a pg_stat_statements CSV or generic SQL query JSON-Lines log",
    )
    import_trace_parser.add_argument(
        "--stack-log",
        dest="stack_log_path",
        metavar="PATH",
        help="Path to a plain-text app log or JSON-Lines record set with Python / JVM / Node.js stack traces",
    )
    import_trace_parser.add_argument(
        "--repo",
        dest="repo",
        default=None,
        help="Repo identifier (owner/name) — defaults to resolving the current directory",
    )
    import_trace_parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Disable PII redaction. Use ONLY for offline debugging on synthetic data.",
    )
    _add_common_args(import_trace_parser)

    # --- init ---
    init_parser = subparsers.add_parser(
        "init",
        help="One-command setup: register with MCP clients, install CLAUDE.md policy, hooks, and index",
    )
    init_parser.add_argument(
        "--client",
        nargs="*",
        default=None,
        metavar="CLIENT",
        help="MCP clients to configure (auto, claude-code, claude-desktop, cursor, windsurf, continue, none)",
    )
    init_parser.add_argument(
        "--claude-md",
        choices=["global", "project"],
        default=None,
        dest="claude_md",
        help="Install Code Exploration Policy to CLAUDE.md (global = ~/.claude/CLAUDE.md, project = ./CLAUDE.md)",
    )
    init_parser.add_argument(
        "--hooks",
        action="store_true",
        help="Install worktree lifecycle hooks into ~/.claude/settings.json",
    )
    init_parser.add_argument(
        "--copilot-hooks",
        action="store_true",
        dest="copilot_hooks",
        help="Write .github/hooks/hooks.json so GitHub Copilot CLI / cloud agent auto-reindex on edit",
    )
    init_parser.add_argument(
        "--index",
        action="store_true",
        help="Index the current working directory after setup",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would be done without making changes",
    )
    init_parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Walk through the full init process without making any changes, "
            "then summarise what would have been done and the benefit of each action"
        ),
    )
    init_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Accept all defaults non-interactively",
    )
    init_parser.add_argument(
        "--no-backup",
        action="store_true",
        dest="no_backup",
        help="Skip creating .bak backups of modified files",
    )
    init_parser.add_argument(
        "--share-savings",
        choices=["on", "off"],
        default=None,
        dest="share_savings",
        help=(
            "Explicitly write share_savings:<on|off> into ~/.code-index/config.jsonc. "
            "Useful for hardened install templates that need a durable opt-out; survives "
            "package upgrades because config --upgrade preserves user-set values."
        ),
    )
    init_parser.add_argument(
        "--no-share-savings",
        action="store_const",
        const="off",
        dest="share_savings",
        help="Shorthand for --share-savings=off.",
    )
    init_parser.add_argument(
        "--minimal",
        action="store_true",
        dest="minimal",
        help=(
            "Write only the MCP server registration; skip every other channel "
            "(CLAUDE.md policy paste, Cursor/Windsurf rules, AGENTS.md, hooks, "
            ".github/hooks, indexing, audit). Recommended for hardened install "
            "templates that don't want jcodemunch touching agent-policy files."
        ),
    )
    init_parser.add_argument(
        "--strict",
        action="store_true",
        dest="strict",
        help=(
            "Enforce munch-first hard: the PreToolUse hook DENIES native Read/Grep "
            "inside an indexed repo (use jcm tools instead). Installs the enforcement "
            "hooks and sets JCODEMUNCH_ENFORCE=strict in ~/.claude/settings.json. "
            "Offset/limit reads and paths outside every indexed repo still pass; "
            "default (no flag) stays advisory warn-only. Revert by re-running init "
            "without --strict."
        ),
    )

    # --- install (per-agent sugar over init) ---
    install_parser = subparsers.add_parser(
        "install",
        help="Per-agent install shortcut. `install claude-code` is sugar for `init --client claude-code --yes`.",
    )
    install_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Agent target: claude-code, claude-desktop, cursor, windsurf, continue, all. "
        "Omit with --list/--status for info-only output.",
    )
    install_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_targets",
        help="List valid install targets and exit",
    )
    install_parser.add_argument(
        "--status",
        action="store_true",
        dest="status",
        help="Print current install state across every target",
    )
    install_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="With --status: emit JSON instead of pretty-printed output",
    )
    install_parser.add_argument(
        "--skills",
        action="store_true",
        dest="skills",
        help="Also emit the jcodemunch Claude Agent Skill bundle (.claude/skills/jcodemunch/SKILL.md)",
    )
    install_parser.add_argument(
        "--skills-scope",
        choices=["global", "project"],
        default="global",
        dest="skills_scope",
        help="Where to write the skill (default: global = ~/.claude/skills/jcodemunch/)",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would happen without making changes",
    )
    install_parser.add_argument(
        "--no-backup",
        action="store_true",
        dest="no_backup",
        help="Skip creating .bak backups of modified files",
    )
    install_parser.add_argument(
        "--share-savings",
        choices=["on", "off"],
        default=None,
        dest="share_savings",
        help=(
            "Explicitly write share_savings:<on|off> into ~/.code-index/config.jsonc. "
            "Survives package upgrades."
        ),
    )
    install_parser.add_argument(
        "--no-share-savings",
        action="store_const",
        const="off",
        dest="share_savings",
        help="Shorthand for --share-savings=off.",
    )
    install_parser.add_argument(
        "--minimal",
        action="store_true",
        dest="minimal",
        help=(
            "Write only the MCP server registration; skip CLAUDE.md, rules, "
            "AGENTS.md, hooks, .github/hooks, indexing, audit."
        ),
    )

    # --- install-status (top-level read-only inspector) ---
    status_parser = subparsers.add_parser(
        "install-status",
        help="Print current install state (clients, policies, hooks).",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit JSON instead of pretty-printed output",
    )

    # --- uninstall ---
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Reverse `init` / `install`: remove jcodemunch entries from configs, policies, and hooks.",
    )
    uninstall_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Agent target to uninstall (claude-code, claude-desktop, cursor, windsurf, continue, all). "
        "Omit to uninstall every detected target plus shared policies and hooks.",
    )
    uninstall_parser.add_argument(
        "--keep-claude-md",
        action="store_true",
        dest="keep_claude_md",
        help="Preserve the CLAUDE.md policy block (do not strip it)",
    )
    uninstall_parser.add_argument(
        "--keep-cursor-rules",
        action="store_true",
        dest="keep_cursor_rules",
        help="Preserve .cursor/rules/jcodemunch.mdc",
    )
    uninstall_parser.add_argument(
        "--keep-windsurf-rules",
        action="store_true",
        dest="keep_windsurf_rules",
        help="Preserve the .windsurfrules policy block",
    )
    uninstall_parser.add_argument(
        "--keep-agents-md",
        action="store_true",
        dest="keep_agents_md",
        help="Preserve the AGENTS.md policy block",
    )
    uninstall_parser.add_argument(
        "--keep-hooks",
        action="store_true",
        dest="keep_hooks",
        help="Preserve jcodemunch hooks in ~/.claude/settings.json",
    )
    uninstall_parser.add_argument(
        "--keep-copilot-hooks",
        action="store_true",
        dest="keep_copilot_hooks",
        help="Preserve the Copilot postToolUse hook in .github/hooks/hooks.json",
    )
    uninstall_parser.add_argument(
        "--keep-skills",
        action="store_true",
        dest="keep_skills",
        help="Preserve the jcodemunch Claude Agent Skill bundle (~/.claude/skills/jcodemunch/)",
    )
    uninstall_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would happen without making changes",
    )
    uninstall_parser.add_argument(
        "--no-backup",
        action="store_true",
        dest="no_backup",
        help="Skip creating .bak backups of modified files",
    )
    uninstall_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Accept all defaults non-interactively",
    )

    # --- hook-event ---
    hook_parser = subparsers.add_parser(
        "hook-event",
        help="Record a Claude Code worktree lifecycle event (used by hooks)",
    )
    hook_parser.add_argument(
        "event_type",
        choices=["create", "remove"],
        help="Event type: 'create' when a worktree is created, 'remove' when deleted",
    )
    _add_common_args(hook_parser)

    # --- hook-pretooluse ---
    subparsers.add_parser(
        "hook-pretooluse",
        help="PreToolUse hook: intercept Read on large code files, suggest jCodemunch (reads stdin)",
    )

    # --- hook-posttooluse ---
    subparsers.add_parser(
        "hook-posttooluse",
        help="PostToolUse hook: auto-reindex files after Edit/Write (reads stdin)",
    )

    # --- hook-copilot-posttooluse ---
    subparsers.add_parser(
        "hook-copilot-posttooluse",
        help="GitHub Copilot postToolUse hook: auto-reindex files after Edit/Write (reads stdin)",
    )

    # --- upgrade ---
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Upgrade jcodemunch-mcp via pip and refresh hooks/config",
    )
    upgrade_parser.add_argument(
        "--no-pip",
        action="store_true",
        dest="no_pip",
        help="Skip 'pip install -U' and only refresh hooks/config",
    )
    upgrade_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Run init refresh non-interactively",
    )

    # --- observatory ---
    obs_parser = subparsers.add_parser(
        "observatory",
        help="Run the public OSS code-health observatory pipeline (static-site output).",
    )
    obs_sub = obs_parser.add_subparsers(dest="obs_action")
    obs_build = obs_sub.add_parser(
        "build", help="Run the full pipeline against a config file."
    )
    obs_build.add_argument(
        "--config", required=True, help="Path to the observatory config JSON."
    )
    obs_build.add_argument(
        "--output-dir", default=None, help="Override config's output_dir."
    )
    obs_build.add_argument("--workdir", default=None, help="Override config's workdir.")
    obs_init = obs_sub.add_parser("init", help="Write a starter config file.")
    obs_init.add_argument(
        "--out",
        default="observatory.config.json",
        help="Where to write the starter config.",
    )

    # --- health ---
    health_parser = subparsers.add_parser(
        "health",
        help="Print get_repo_health JSON to stdout (includes six-axis radar). For CI / scripting.",
    )
    health_parser.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Repo identifier (path, owner/name, or bare display name). Defaults to '.' (cwd).",
    )
    health_parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Churn look-back window in days (default 90).",
    )
    health_parser.add_argument(
        "--radar-only",
        action="store_true",
        help="Emit only the `radar` sub-field instead of the full health response.",
    )
    health_parser.add_argument(
        "--storage-path", default=None, help="Override index storage location."
    )

    # --- receipt ---
    receipt_parser = subparsers.add_parser(
        "receipt",
        help="Token-economy ledger: parse Claude transcripts, show modeled tokens-saved + dollar value",
    )
    receipt_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Window size in days (default 30; use 0 for all-time).",
    )
    receipt_parser.add_argument(
        "--model",
        choices=["sonnet", "opus", "haiku"],
        default="opus",
        help="Model rate to apply for the dollar conversion (default opus).",
    )
    receipt_parser.add_argument(
        "--export",
        metavar="FILE.csv|FILE.json",
        default=None,
        help="Write raw per-tool data to a file instead of the human report.",
    )
    receipt_parser.add_argument(
        "--explain",
        action="store_true",
        help="Print the per-tool savings multiplier table + methodology, then exit.",
    )
    receipt_parser.add_argument(
        "--projects-root",
        default=None,
        help="Override Claude Code projects directory (default ~/.claude/projects).",
    )

    # --- whatsnew ---
    whatsnew_parser = subparsers.add_parser(
        "whatsnew",
        help="Refresh README recency block + write whatsnew.json from CHANGELOG.md (release flow)",
    )
    whatsnew_parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (default: cwd)",
    )
    whatsnew_parser.add_argument(
        "--max-entries",
        type=int,
        default=3,
        help="Number of recent releases to include (default 3)",
    )

    # --- hook-precompact ---
    subparsers.add_parser(
        "hook-precompact",
        help="PreCompact hook: generate session snapshot before context compaction (reads stdin)",
    )

    # --- hook-taskcomplete ---
    subparsers.add_parser(
        "hook-taskcomplete",
        help="TaskCompleted hook: post-task diagnostics — dead code, untested symbols, dangling refs (reads stdin)",
    )

    # --- hook-subagent-start ---
    subparsers.add_parser(
        "hook-subagent-start",
        help="SubagentStart hook: inject condensed repo orientation for spawned agents (reads stdin)",
    )

    # --- watch-claude ---
    wc_parser = subparsers.add_parser(
        "watch-claude",
        help="Auto-discover and watch Claude Code worktrees",
    )
    wc_parser.add_argument(
        "--repos",
        nargs="+",
        help="One or more git repository paths to poll for worktrees via `git worktree list`",
    )
    wc_parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Poll interval in seconds (default: from config, also via JCODEMUNCH_CLAUDE_POLL_INTERVAL)",
    )
    wc_parser.add_argument(
        "--debounce",
        type=int,
        default=None,
        metavar="MS",
        help="Debounce interval in ms for file watching (default: from config, also via JCODEMUNCH_WATCH_DEBOUNCE_MS)",
    )
    wc_parser.add_argument(
        "--no-ai-summaries",
        action="store_true",
        help="Disable AI-generated summaries during re-indexing",
    )
    wc_parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Include symlinked files in indexing",
    )
    wc_parser.add_argument(
        "--extra-ignore",
        nargs="*",
        help="Additional gitignore-style patterns to exclude",
    )
    _add_common_args(wc_parser)

    # --- watch-all ---
    wa_parser = subparsers.add_parser(
        "watch-all",
        help="Auto-discover every locally-indexed repo and auto-reindex on change",
    )
    wa_parser.add_argument(
        "--debounce",
        type=int,
        default=None,
        metavar="MS",
        help="Debounce interval in ms (default: from config)",
    )
    wa_parser.add_argument(
        "--rediscover-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Re-scan the index registry for new/removed repos every N seconds (default: 30)",
    )
    wa_parser.add_argument(
        "--no-ai-summaries",
        action="store_true",
        help="Disable AI-generated summaries during re-indexing",
    )
    wa_parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Include symlinked files in indexing",
    )
    wa_parser.add_argument(
        "--extra-ignore",
        nargs="*",
        help="Additional gitignore-style patterns to exclude",
    )
    _add_common_args(wa_parser)

    # --- watch-install / watch-uninstall / watch-status ---
    _add_common_args(
        subparsers.add_parser(
            "watch-install",
            help="Install watch-all as a login service (systemd/launchd/Task Scheduler)",
        )
    )
    _add_common_args(
        subparsers.add_parser(
            "watch-uninstall",
            help="Remove the installed watch-all login service",
        )
    )
    _add_common_args(
        subparsers.add_parser(
            "watch-status",
            help="Print watch-all service state + per-repo reindex status",
        )
    )

    # --- keyring (P1.3) ---
    keyring_parser = subparsers.add_parser(
        "keyring",
        help="Manage credentials in the system keyring (macOS Keychain / Windows Credential Manager / freedesktop Secret Service). Requires the [keyring] extra.",
    )
    keyring_sub = keyring_parser.add_subparsers(dest="keyring_action")
    keyring_set_p = keyring_sub.add_parser(
        "set", help="Store a credential. Prompts for the value via getpass."
    )
    keyring_set_p.add_argument(
        "name", help="Env-var name the credential maps to (e.g. ANTHROPIC_API_KEY)"
    )
    keyring_set_p.add_argument(
        "--from-env",
        action="store_true",
        help="Read the value from the current env var instead of prompting.",
    )
    keyring_get_p = keyring_sub.add_parser(
        "get", help="Print a stored credential to stdout (sensitive — pipe with care)."
    )
    keyring_get_p.add_argument("name", help="Env-var name the credential maps to")
    keyring_del_p = keyring_sub.add_parser("delete", help="Remove a stored credential.")
    keyring_del_p.add_argument("name", help="Env-var name the credential maps to")
    keyring_sub.add_parser(
        "list",
        help="List the credential env-var names jcodemunch recognises for keyring lookup.",
    )

    # --- download-model ---
    dm_parser = subparsers.add_parser(
        "download-model",
        help="Download the bundled ONNX embedding model (all-MiniLM-L6-v2) for zero-config semantic search",
    )
    dm_parser.add_argument(
        "--target-dir",
        default=None,
        metavar="PATH",
        help="Custom directory to store the model (default: ~/.code-index/models/all-MiniLM-L6-v2/)",
    )

    # --- install-pack ---
    ip_parser = subparsers.add_parser(
        "install-pack",
        help="Download and install a Starter Pack pre-built index",
    )
    ip_parser.add_argument(
        "pack_id",
        nargs="?",
        default=None,
        help="Pack identifier to install (e.g. nodejs, fastapi)",
    )
    ip_parser.add_argument(
        "--license",
        default=None,
        dest="license_key",
        metavar="KEY",
        help="jCodeMunch license key (required for premium packs)",
    )
    ip_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_packs",
        help="List all available starter packs",
    )
    ip_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and overwrite an already-installed pack",
    )

    # Backwards compat: if first non-flag arg isn't a known subcommand,
    # prepend "serve" so legacy invocations like `jcodemunch-mcp --transport sse` still work.
    # But let --help and -V be handled by the top-level parser first.
    raw_argv = argv if argv is not None else sys.argv[1:]
    top_level_flags = {"-h", "--help", "-V", "--version"}
    if any(arg in top_level_flags for arg in raw_argv):
        args = parser.parse_args(raw_argv)
    else:
        known_commands = {
            "serve",
            "watch",
            "hook-event",
            "hook-pretooluse",
            "hook-posttooluse",
            "hook-copilot-posttooluse",
            "hook-precompact",
            "hook-taskcomplete",
            "hook-subagent-start",
            "watch-claude",
            "watch-all",
            "watch-install",
            "watch-uninstall",
            "watch-status",
            "config",
            "list-repos",
            "delete-index",
            "org-report",
            "org-rollup",
            "license",
            "index",
            "index-file",
            "import-trace",
            "claude-md",
            "init",
            "install",
            "install-status",
            "uninstall",
            "install-pack",
            "download-model",
            "upgrade",
            "whatsnew",
            "receipt",
            "health",
            "observatory",
            "keyring",
        }
        # MCP-tool-name typos: route to the right CLI verb with a friendly hint.
        # `index_repo` and `index_folder` are MCP tools, not CLI subcommands.
        _CLI_ALIASES = {
            "index_repo": "index",
            "index-repo": "index",
            "index_folder": "index",
            "index-folder": "index",
            "index_file": "index-file",
        }
        first_pos = next((a for a in raw_argv if not a.startswith("-")), None)
        if first_pos in _CLI_ALIASES:
            target = _CLI_ALIASES[first_pos]
            print(
                f"jcodemunch-mcp: error: unknown subcommand `{first_pos}`. Did you mean:\n"
                f"    jcodemunch-mcp {target} <owner/repo>\n"
                f"    jcodemunch-mcp {target} <github-url>\n"
                f"    jcodemunch-mcp {target} <local-path>",
                file=sys.stderr,
            )
            sys.exit(2)
        has_subcommand = any(
            arg in known_commands for arg in raw_argv if not arg.startswith("-")
        )
        if not has_subcommand:
            raw_argv = ["serve"] + list(raw_argv)
        args = parser.parse_args(raw_argv)

    # P1.3 keyring resolution: rewrite any `keyring:NAME` env-var values to
    # the actual secret stored under that name in the system keyring. Runs
    # before any subcommand dispatch so all downstream code that calls
    # os.environ.get("ANTHROPIC_API_KEY") etc. sees the resolved value.
    # Skipped for the `keyring` subcommand itself (no point resolving env
    # vars when the user is about to manage them).
    if getattr(args, "command", None) != "keyring":
        try:
            from . import credentials as _creds

            _creds.resolve_credentials_in_env()
        except Exception:
            logger.debug("credential env resolution skipped", exc_info=True)

    if args.command == "config":
        action = getattr(args, "action", None)
        if action in ("set", "unset"):
            from . import config as _cfg

            as_json = getattr(args, "json", False)
            key = getattr(args, "key", None)
            if not key:
                _emit = (
                    (lambda d: print(json.dumps(d, indent=2)))
                    if as_json
                    else (lambda d: print(d.get("error", ""), file=sys.stderr))
                )
                _emit({"success": False, "error": f"config {action} requires a key"})
                sys.exit(2)
            try:
                if action == "set":
                    if getattr(args, "value", None) is None:
                        raise ValueError("config set requires a value")
                    written = _cfg.set_config_value(key, args.value)
                    result = {
                        "success": True,
                        "key": key,
                        "value": written,
                        "message": f"set {key} = {json.dumps(written)}",
                    }
                else:
                    changed = _cfg.unset_config_value(key)
                    result = {
                        "success": True,
                        "key": key,
                        "changed": changed,
                        "message": (
                            f"cleared {key} (default applies)"
                            if changed
                            else f"{key} was not set"
                        ),
                    }
            except ValueError as e:
                if as_json:
                    print(
                        json.dumps(
                            {"success": False, "key": key, "error": str(e)}, indent=2
                        )
                    )
                else:
                    print(f"error: {e}", file=sys.stderr)
                sys.exit(1)
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                print(result["message"])
            return
        if getattr(args, "json", False):
            from . import config as _cfg

            print(json.dumps(_cfg.config_report(repo=str(Path.cwd())), indent=2))
            return
        _run_config(
            check=getattr(args, "check", False),
            init=getattr(args, "init", False),
            upgrade=getattr(args, "upgrade", False),
        )
        return

    if args.command == "org-report":
        from .org.report import run_org_report

        res = run_org_report(
            model=getattr(args, "model", "opus"),
            org_id=getattr(args, "org", None),
            seat_id=getattr(args, "seat", None),
            endpoint=getattr(args, "endpoint", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        elif res.get("error"):
            print(f"error: {res['error']}", file=sys.stderr)
        elif res.get("reported") is False:
            print(
                f"report to {res.get('endpoint')} failed: {res.get('error')}",
                file=sys.stderr,
            )
        else:
            via = (
                "posted to " + res["endpoint"]
                if res.get("transport") == "http"
                else "recorded locally"
            )
            print(
                f"seat {res['seat_id']} in org {res['org_id']} ({via}): "
                f"{res['tokens_saved']} tokens, ${res['usd']:.2f}, {res['calls']} calls"
            )
        return

    if args.command == "org-rollup":
        from .org.license import check_gate
        from .org.store import org_rollup

        org = getattr(args, "org", None) or os.environ.get("JCODEMUNCH_ORG_ID", "")
        as_json = getattr(args, "json", False)
        if not org:
            print("error: provide --org or set JCODEMUNCH_ORG_ID", file=sys.stderr)
            return

        # org-rollup is the team-SKU (paid) feature — gate it. Individual tools
        # are untouched; seat reporting stays free so trial data accrues.
        gate = check_gate()
        if not gate["allowed"]:
            if as_json:
                print(
                    json.dumps(
                        {
                            "error": gate["reason"],
                            "license_mode": gate["mode"],
                            "get_license": gate["get_license"],
                        },
                        indent=2,
                    )
                )
            else:
                print(f"org-rollup is unavailable: {gate['reason']}", file=sys.stderr)
                print(f"  Get a license: {gate['get_license']}", file=sys.stderr)
            return
        if gate["mode"] == "grace":
            print(f"note: {gate['reason']}  ({gate['get_license']})", file=sys.stderr)

        data = org_rollup(org)
        data["_license"] = {
            "mode": gate["mode"],
            "tier": gate.get("tier"),
            "grace_days_left": gate.get("grace_days_left"),
            "key": gate.get("key_masked"),
        }
        if as_json:
            print(json.dumps(data, indent=2))
        else:
            t = data["totals"]
            print(
                f"org {org}: {t['seat_count']} seats · {t['tokens_saved']} tokens · ${t['usd']:.2f} · {t['calls']} calls"
            )
            for s in data["seats"]:
                print(
                    f"  {s['seat_id']:<24} {s['tokens_saved']:>9} tok  ${s['usd']:>8.2f}  {s['calls']:>5} calls"
                )
        return

    if args.command == "license":
        from .org.license import check_gate

        key = getattr(args, "key", None)
        if key:
            os.environ["JCODEMUNCH_LICENSE_KEY"] = key  # validate this key for this run
        gate = check_gate()
        if getattr(args, "json", False):
            print(json.dumps(gate, indent=2))
        else:
            mode = gate["mode"]
            label = {
                "licensed": "licensed",
                "grace": "evaluation (unlicensed)",
                "blocked": "unlicensed",
            }[mode]
            print(f"License: {label}")
            if gate.get("key_masked"):
                print(f"  Key:   {gate['key_masked']}")
            if gate.get("tier"):
                print(f"  Tier:  {gate['tier']}")
            if mode == "grace":
                print(f"  Trial: {gate['grace_days_left']} day(s) left")
            print(f"  {gate['reason']}")
            if gate.get("get_license"):
                print(f"  Get a license: {gate['get_license']}")
            if key:
                print(
                    "  (set JCODEMUNCH_LICENSE_KEY or config `license_key` to persist this key)"
                )
        return

    if args.command == "list-repos":
        from .tools.list_repos import repos_report

        report = repos_report(storage_path=os.environ.get("CODE_INDEX_PATH"))
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
        elif not report:
            print("No indexed repositories.")
        else:
            for r in report:
                langs = ", ".join(f"{k}:{v}" for k, v in sorted(r["languages"].items()))
                print(
                    f"{r['display_name']:<28} {r['symbol_count']:>6} sym  "
                    f"{r['file_count']:>5} files  {r['freshness']:<16} "
                    f"watcher={r['watcher_state']}" + (f"  [{langs}]" if langs else "")
                )
        return

    if args.command == "delete-index":
        # Delete a repo's index + cached data.
        # Exit non-zero on failure so callers (e.g. the jMunch Console) can
        # detect it via the return code, not just the JSON body.
        from .storage.index_store import IndexStore

        storage_path = os.environ.get("CODE_INDEX_PATH")
        try:
            store = IndexStore(base_path=storage_path)
            repo_arg = args.repo
            # Parse owner/name from repo arg
            if "/" in repo_arg:
                owner, name = repo_arg.split("/", 1)
            else:
                owner, name = "", repo_arg
            deleted = store.delete_index(owner, name)
            result = {"success": deleted, "repo": repo_arg}
            if deleted:
                result["message"] = f"Deleted index for {repo_arg}"
            else:
                result["error"] = f"No index found for {repo_arg}"
        except Exception as e:
            result = {"success": False, "repo": args.repo, "error": str(e)}
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        elif result.get("success"):
            print(result.get("message", f"Deleted index for {args.repo}"))
        else:
            print(
                result.get("error", f"No index found for {args.repo}"), file=sys.stderr
            )
        sys.exit(0 if result.get("success") else 1)

    if args.command == "claude-md":
        _run_claude_md(
            generate=getattr(args, "generate", False),
            fmt=getattr(args, "fmt", "full"),
        )
        return

    if args.command == "init":
        from .cli.init import run_init

        sys.exit(
            run_init(
                clients=args.client,
                claude_md=args.claude_md,
                hooks=args.hooks,
                copilot_hooks=getattr(args, "copilot_hooks", False),
                index=args.index,
                dry_run=args.dry_run,
                demo=args.demo,
                yes=args.yes,
                no_backup=args.no_backup,
                share_savings=getattr(args, "share_savings", None),
                minimal=getattr(args, "minimal", False),
                strict=getattr(args, "strict", False),
            )
        )

    if args.command == "install":
        from .cli.init import (
            _AGENT_ALIASES,
            run_init,
        )
        from .cli.init import (
            install_status as _install_status,
        )
        from .cli.init import (
            list_targets as _list_targets,
        )
        from .cli.init import (
            print_status as _print_status,
        )

        if getattr(args, "list_targets", False):
            _list_targets()
            sys.exit(0)
        if getattr(args, "status", False):
            _print_status(_install_status(), as_json=getattr(args, "as_json", False))
            sys.exit(0)
        target = args.target
        if not target:
            print(
                "install: please pass a target (e.g. `install claude-code`),\n"
                "        or use --list / --status for info-only output.",
                file=sys.stderr,
            )
            sys.exit(2)
        if target.lower() not in _AGENT_ALIASES:
            print(
                f"install: unknown target '{target}'. Valid: "
                f"{', '.join(sorted(_AGENT_ALIASES))}",
                file=sys.stderr,
            )
            sys.exit(2)
        client_arg = None if target.lower() == "all" else [target.lower()]
        sys.exit(
            run_init(
                clients=client_arg or ["auto"],
                claude_md="global",
                hooks=True,
                copilot_hooks=False,
                index=False,
                dry_run=getattr(args, "dry_run", False),
                demo=False,
                yes=True,
                no_backup=getattr(args, "no_backup", False),
                skills=getattr(args, "skills", False),
                skills_scope=getattr(args, "skills_scope", "global"),
                share_savings=getattr(args, "share_savings", None),
                minimal=getattr(args, "minimal", False),
            )
        )

    if args.command == "install-status":
        from .cli.init import install_status as _install_status
        from .cli.init import print_status as _print_status

        _print_status(_install_status(), as_json=getattr(args, "as_json", False))
        sys.exit(0)

    if args.command == "uninstall":
        from .cli.init import run_uninstall

        sys.exit(
            run_uninstall(
                target=args.target,
                claude_md=not getattr(args, "keep_claude_md", False),
                cursor_rules=not getattr(args, "keep_cursor_rules", False),
                windsurf_rules=not getattr(args, "keep_windsurf_rules", False),
                agents_md=not getattr(args, "keep_agents_md", False),
                hooks=not getattr(args, "keep_hooks", False),
                copilot_hooks=not getattr(args, "keep_copilot_hooks", False),
                skills=not getattr(args, "keep_skills", False),
                dry_run=getattr(args, "dry_run", False),
                no_backup=getattr(args, "no_backup", False),
                yes=getattr(args, "yes", False),
            )
        )

    if args.command == "keyring":
        import getpass as _getpass

        from . import credentials as _creds

        action = getattr(args, "keyring_action", None)
        if action is None:
            print(
                "keyring: please pass a subcommand (set/get/delete/list)",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            if action == "set":
                name = args.name
                if getattr(args, "from_env", False):
                    value = os.environ.get(name, "")
                    if not value:
                        print(
                            f"keyring set: env var {name} is empty or unset",
                            file=sys.stderr,
                        )
                        sys.exit(2)
                else:
                    value = _getpass.getpass(f"Enter value for {name}: ")
                if not value:
                    print("keyring set: empty value, aborted", file=sys.stderr)
                    sys.exit(2)
                _creds.keyring_set(name, value)
                print(
                    f"Stored {name} in system keyring under service '{_creds.SERVICE_NAME}'."
                )
                print(f"To use it, set: {name}=keyring:{name}  (in your MCP env block)")
                sys.exit(0)
            elif action == "get":
                value = _creds.keyring_get(args.name)
                if value is None:
                    print(
                        f"No keyring entry for {args.name} under service '{_creds.SERVICE_NAME}'."
                    )
                    sys.exit(1)
                print(value)
                sys.exit(0)
            elif action == "delete":
                removed = _creds.keyring_delete(args.name)
                if removed:
                    print(f"Removed {args.name} from system keyring.")
                else:
                    print(
                        f"No keyring entry for {args.name} to remove (or removal failed)."
                    )
                sys.exit(0 if removed else 1)
            elif action == "list":
                print(
                    "Recognised credential env-var names (set any to keyring:<name> to enable keyring resolution):"
                )
                for var in _creds.list_recognised_env_vars():
                    populated = _creds.keyring_get(var)
                    state = "stored" if populated else "not set"
                    print(f"  {var:<30}  {state}")
                sys.exit(0)
            else:
                print(f"keyring: unknown subcommand '{action}'", file=sys.stderr)
                sys.exit(2)
        except ImportError as e:
            print(f"keyring: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"keyring {action} failed: {e}", file=sys.stderr)
            sys.exit(1)

    if args.command == "download-model":
        from pathlib import Path as _Path

        from .embeddings.local_encoder import download_model as _download_model

        try:
            target = _Path(args.target_dir) if args.target_dir else None
            _download_model(target)
            sys.exit(0)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)  # noqa: T201
            sys.exit(1)

    if args.command == "install-pack":
        from .cli.install_pack import run_install_pack

        sys.exit(
            run_install_pack(
                pack_id=args.pack_id,
                license_key=args.license_key,
                list_packs=args.list_packs,
                force=args.force,
            )
        )

    if args.command == "hook-pretooluse":
        from .cli.hooks import run_pretooluse

        sys.exit(run_pretooluse())

    if args.command == "hook-posttooluse":
        from .cli.hooks import run_posttooluse

        sys.exit(run_posttooluse())

    if args.command == "hook-copilot-posttooluse":
        from .cli.hooks import run_copilot_posttooluse

        sys.exit(run_copilot_posttooluse())

    if args.command == "upgrade":
        from .cli.upgrade import run_upgrade

        sys.exit(run_upgrade(no_pip=args.no_pip, yes=args.yes))

    if args.command == "whatsnew":
        from .cli.whatsnew import main as whatsnew_main

        sys.exit(
            whatsnew_main(
                [
                    "--repo-root",
                    args.repo_root,
                    "--max-entries",
                    str(args.max_entries),
                ]
            )
        )

    if args.command == "observatory":
        from .cli.observatory import main as observatory_main

        argv = []
        if args.obs_action == "build":
            argv = ["build", "--config", args.config]
            if args.output_dir:
                argv += ["--output-dir", args.output_dir]
            if args.workdir:
                argv += ["--workdir", args.workdir]
        elif args.obs_action == "init":
            argv = ["init", "--out", args.out]
        else:
            argv = []
        sys.exit(observatory_main(argv))

    if args.command == "health":
        from .cli.health import main as health_main

        argv = [args.repo, "--days", str(args.days)]
        if args.radar_only:
            argv += ["--radar-only"]
        if args.storage_path:
            argv += ["--storage-path", args.storage_path]
        sys.exit(health_main(argv))

    if args.command == "receipt":
        from .cli.receipt import main as receipt_main

        argv = ["--days", str(args.days), "--model", args.model]
        if args.export:
            argv += ["--export", args.export]
        if args.explain:
            argv += ["--explain"]
        if args.projects_root:
            argv += ["--projects-root", args.projects_root]
        sys.exit(receipt_main(argv))

    if args.command == "hook-precompact":
        from .cli.hooks import run_precompact

        sys.exit(run_precompact())

    if args.command == "hook-taskcomplete":
        from .cli.hooks import run_taskcomplete

        sys.exit(run_taskcomplete())

    if args.command == "hook-subagent-start":
        from .cli.hooks import run_subagentstart

        sys.exit(run_subagentstart())

    # Apply config defaults for watcher keys: CLI args > config > env vars.
    # config.load_config() is called inside each subcommand handler, but we need
    # the values here to fill in None defaults from argparse.
    # load_config() is idempotent so calling it early is safe.
    config_module.load_config()

    # --watcher-debounce (serve subcommand) / --debounce (watch, watch-claude)
    # Only set if the attr exists on args and is None (not explicitly provided on CLI)
    _debounce = config_module.get("watch_debounce_ms", 2000)
    if getattr(args, "watcher_debounce", None) is None:
        args.watcher_debounce = _debounce
    if getattr(args, "debounce", None) is None:
        args.debounce = _debounce

    # --poll-interval (watch-claude subcommand)
    if getattr(args, "poll_interval", None) is None:
        args.poll_interval = config_module.get("claude_poll_interval", 5.0)

    # --freshness-mode is only relevant for serve subcommand; handled there

    _setup_logging(args)

    if args.command == "watch":
        use_ai = not args.no_ai_summaries and _default_use_ai_summaries()
        if args.once:
            from .watcher import sync_folders

            asyncio.run(
                sync_folders(
                    paths=args.paths,
                    use_ai_summaries=use_ai,
                    storage_path=os.environ.get("CODE_INDEX_PATH"),
                    extra_ignore_patterns=args.extra_ignore,
                    follow_symlinks=args.follow_symlinks,
                )
            )
        else:
            from .watcher import watch_folders

            asyncio.run(
                watch_folders(
                    paths=args.paths,
                    debounce_ms=args.debounce,
                    use_ai_summaries=use_ai,
                    storage_path=os.environ.get("CODE_INDEX_PATH"),
                    extra_ignore_patterns=args.extra_ignore,
                    follow_symlinks=args.follow_symlinks,
                    idle_timeout_minutes=args.idle_timeout,
                )
            )
    elif args.command == "hook-event":
        from .hook_event import handle_hook_event

        handle_hook_event(event_type=args.event_type)
    elif args.command == "watch-all":
        from .watch_all import DEFAULT_REDISCOVER_INTERVAL_S, watch_all

        use_ai = not args.no_ai_summaries and _default_use_ai_summaries()
        asyncio.run(
            watch_all(
                debounce_ms=args.debounce
                or int(os.environ.get("JCODEMUNCH_WATCH_DEBOUNCE_MS", "200")),
                use_ai_summaries=use_ai,
                storage_path=os.environ.get("CODE_INDEX_PATH"),
                extra_ignore_patterns=args.extra_ignore,
                follow_symlinks=args.follow_symlinks,
                rediscover_interval_s=args.rediscover_interval
                or DEFAULT_REDISCOVER_INTERVAL_S,
            )
        )
    elif args.command == "watch-install":
        import json as _json

        from .service_installer import InstallerError, install_service

        try:
            print(_json.dumps(install_service(), indent=2))
        except InstallerError as exc:
            print(f"watch-install failed: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "watch-uninstall":
        import json as _json

        from .service_installer import InstallerError, uninstall_service

        try:
            print(_json.dumps(uninstall_service(), indent=2))
        except InstallerError as exc:
            print(f"watch-uninstall failed: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "watch-status":
        import json as _json

        from .service_installer import service_status

        result = service_status()
        print(_json.dumps(result, indent=2))
    elif args.command == "watch-claude":
        from .watcher import watch_claude_worktrees

        use_ai = not args.no_ai_summaries and _default_use_ai_summaries()
        asyncio.run(
            watch_claude_worktrees(
                repos=args.repos,
                poll_interval=args.poll_interval,
                debounce_ms=args.debounce,
                use_ai_summaries=use_ai,
                storage_path=os.environ.get("CODE_INDEX_PATH"),
                extra_ignore_patterns=args.extra_ignore,
                follow_symlinks=args.follow_symlinks,
            )
        )
    elif args.command == "index":
        import json as _json

        t = args.target
        use_ai = not args.no_ai_summaries and _default_use_ai_summaries()

        # `--paths-from FILE | -` reads one path per line; comments (`# ...`)
        # and blank lines are stripped. Empty input is a hard error so the
        # command doesn't silently fall through to a full-tree index.
        paths_arg: Optional[list] = None
        paths_from = getattr(args, "paths_from", None)
        if paths_from:
            paths_arg, _err = _load_index_paths_from_arg(paths_from)
            if _err is not None:
                print(_json.dumps({"success": False, "error": _err}, indent=2))
                sys.exit(1)

        # Heuristic: local paths start with /, ., or a Windows drive letter.
        # Everything else (owner/repo, github.com/owner/repo, https://github.com/...,
        # git@github.com:owner/repo) routes to the GitHub indexer, which calls
        # parse_github_url for normalization.
        is_local = (
            "/" not in t
            or t.startswith("/")
            or t.startswith(".")
            or (len(t) > 1 and t[1] == ":")
        )
        if is_local:
            from .tools.index_folder import index_folder as _index_folder

            result = _index_folder(
                path=t,
                use_ai_summaries=use_ai,
                storage_path=os.environ.get("CODE_INDEX_PATH"),
                extra_ignore_patterns=args.extra_ignore,
                follow_symlinks=args.follow_symlinks,
                paths=paths_arg,
            )
        else:
            if paths_arg is not None:
                print(
                    _json.dumps(
                        {
                            "success": False,
                            "error": "--paths-from is only supported for local targets, not GitHub repos.",
                        },
                        indent=2,
                    )
                )
                sys.exit(1)
            from .tools.index_repo import index_repo as _index_repo

            result = asyncio.run(
                _index_repo(
                    url=t,
                    use_ai_summaries=use_ai,
                    storage_path=os.environ.get("CODE_INDEX_PATH"),
                )
            )
        print(_json.dumps(result, indent=2))
        if not result.get("success"):
            sys.exit(1)
    elif args.command == "index-file":
        import json as _json

        from .tools.index_file import index_file as _index_file

        use_ai = not args.no_ai_summaries and _default_use_ai_summaries()
        result = _index_file(
            path=args.path,
            use_ai_summaries=use_ai,
            storage_path=os.environ.get("CODE_INDEX_PATH"),
        )
        print(_json.dumps(result, indent=2))
        if not result.get("success"):
            sys.exit(1)
    elif args.command == "import-trace":
        import json as _json

        from .tools.import_runtime_signal import (
            import_runtime_signal as _import_runtime_signal,
        )

        otel_path = getattr(args, "otel_path", None)
        sql_log_path = getattr(args, "sql_log_path", None)
        stack_log_path = getattr(args, "stack_log_path", None)
        provided = [p for p in (otel_path, sql_log_path, stack_log_path) if p]
        if not provided:
            print(
                "jcodemunch-mcp: error: import-trace requires one of --otel / --sql-log / --stack-log <path>",
                file=sys.stderr,
            )
            sys.exit(2)
        if len(provided) > 1:
            print(
                "jcodemunch-mcp: error: import-trace accepts exactly one of --otel / --sql-log / --stack-log. "
                "Run the command once per source if you have multiple.",
                file=sys.stderr,
            )
            sys.exit(2)
        if otel_path:
            source = "otel"
            trace_path = otel_path
        elif sql_log_path:
            source = "sql_log"
            trace_path = sql_log_path
        else:
            source = "stack_log"
            trace_path = stack_log_path
        result = _import_runtime_signal(
            source=source,
            path=trace_path,
            repo=args.repo,
            redact_enabled=not args.no_redact,
            storage_path=os.environ.get("CODE_INDEX_PATH"),
        )
        print(_json.dumps(result, indent=2))
        if not result.get("success", True):
            sys.exit(1)
    else:
        # serve (default)
        # Re-run load_config() after _setup_logging() so config warnings/errors
        # go to the configured log destination (the early call at startup ran before logging was set up)
        config_module.load_config()

        # Version-drift probe: warn if `pip install -U` ran but `init` did not.
        # Stale hook templates can point at older binaries / event names.
        try:
            from . import __version__ as _current_version
            from .cli.init import read_install_version

            _stamped = read_install_version()
            if (
                _stamped
                and _stamped != _current_version
                and _current_version != "unknown"
            ):
                logger.warning(
                    "jcodemunch-mcp upgraded %s -> %s but `init` has not been "
                    "re-run. Hook templates and config may be stale; run "
                    "`jcodemunch-mcp upgrade` (or `init --hooks`) to refresh.",
                    _stamped,
                    _current_version,
                )
        except Exception:
            logger.debug("install-version probe failed", exc_info=True)

        # Clean up orphan indexes whose source_root no longer exists
        try:
            from .storage import IndexStore

            storage_path = os.environ.get("CODE_INDEX_PATH")
            store = IndexStore(base_path=storage_path)
            cleaned = store.cleanup_orphan_indexes()
            store.close()
            if cleaned:
                logger.info("Cleaned up %d orphan index(es)", cleaned)
        except Exception:
            logger.debug("Orphan index cleanup failed", exc_info=True)

        config_module.load_all_project_configs()
        from .reindex_state import set_freshness_mode

        # Apply config default if --freshness-mode was not explicitly provided
        if args.freshness_mode is None:
            args.freshness_mode = config_module.get("freshness_mode", "relaxed")
        set_freshness_mode(args.freshness_mode)
        watcher_enabled = _get_watcher_enabled(args)
        watcher_from_cli = getattr(args, "watcher", None) is not None

        if watcher_enabled:
            try:
                import watchfiles  # noqa: F401
            except ImportError:
                if watcher_from_cli:
                    print(
                        "ERROR: --watcher requires watchfiles. "
                        "Install with: pip install 'jcodemunch-mcp[watch]'",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                logger.warning(
                    "watch is enabled in config but the 'watchfiles' "
                    "package is not installed; continuing without the "
                    "file watcher. Install with: pip install "
                    "'jcodemunch-mcp[watch]'"
                )
                watcher_enabled = False

        if watcher_enabled:
            # Watcher params: CLI flag > config > default
            cfg_paths = config_module.get("watch_paths", [])
            if args.watcher_path is not None:
                watcher_paths = args.watcher_path
            elif cfg_paths:
                watcher_paths = cfg_paths
            else:
                watcher_paths = [os.getcwd()]

            use_ai = not args.watcher_no_ai_summaries and _default_use_ai_summaries()

            watcher_kwargs = dict(
                paths=watcher_paths,
                debounce_ms=(
                    args.watcher_debounce
                    if args.watcher_debounce is not None
                    else config_module.get("watch_debounce_ms", 2000)
                ),
                use_ai_summaries=use_ai,
                storage_path=os.environ.get("CODE_INDEX_PATH"),
                extra_ignore_patterns=(
                    args.watcher_extra_ignore
                    if args.watcher_extra_ignore is not None
                    else config_module.get("watch_extra_ignore", []) or None
                ),
                follow_symlinks=(
                    args.watcher_follow_symlinks
                    or config_module.get("watch_follow_symlinks", False)
                ),
                idle_timeout_minutes=(
                    args.watcher_idle_timeout
                    if args.watcher_idle_timeout is not None
                    else config_module.get("watch_idle_timeout", None)
                ),
            )

            log_path = getattr(args, "watcher_log", None) or config_module.get(
                "watch_log", None
            )

            try:
                if args.transport == "sse":
                    asyncio.run(
                        _run_server_with_watcher(
                            run_sse_server,
                            (args.host, args.port),
                            watcher_kwargs,
                            log_path,
                        )
                    )
                elif args.transport == "streamable-http":
                    asyncio.run(
                        _run_server_with_watcher(
                            run_streamable_http_server,
                            (args.host, args.port),
                            watcher_kwargs,
                            log_path,
                        )
                    )
                else:
                    asyncio.run(
                        _run_server_with_watcher(
                            run_stdio_server,
                            (),
                            watcher_kwargs,
                            log_path,
                        )
                    )
            except KeyboardInterrupt:
                pass
        else:
            if args.transport == "sse":
                asyncio.run(run_sse_server(args.host, args.port))
            elif args.transport == "streamable-http":
                asyncio.run(run_streamable_http_server(args.host, args.port))
            else:
                asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()

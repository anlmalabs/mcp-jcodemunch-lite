"""Unified code/docs reading surface.

This module is the integration boundary between the existing jCodeMunch code
index and the vendored jDocMunch documentation index. It deliberately delegates
to the mature domain tools instead of reimplementing parsing, ranking, or
storage behavior here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_DOC_STRONG_EXTS = {
    ".md",
    ".markdown",
    ".mdx",
    ".txt",
    ".rst",
    ".adoc",
    ".asciidoc",
    ".asc",
    ".ipynb",
    ".html",
    ".htm",
}

_DOC_AMBIGUOUS_EXTS = {
    ".yaml",
    ".yml",
    ".json",
    ".jsonc",
    ".xml",
    ".svg",
    ".xhtml",
    ".tscn",
    ".tres",
}


def _doc_storage_path(explicit: Optional[str] = None) -> Optional[str]:
    return explicit if explicit is not None else os.environ.get("DOC_INDEX_PATH")


def _same_source_root(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(os.path.expanduser(left)) == os.path.realpath(
        os.path.expanduser(right)
    )


def _normalize_domain(domain: Optional[str]) -> str:
    value = (domain or "auto").strip().lower()
    if value in {"doc", "documentation"}:
        return "docs"
    if value not in {"auto", "both", "code", "docs"}:
        raise ValueError("domain must be one of: auto, both, code, docs")
    return value


def _strip_unit_prefix(unit_id: str) -> tuple[Optional[str], str]:
    if unit_id.startswith("code:"):
        return "code", unit_id[len("code:") :]
    if unit_id.startswith("doc:"):
        return "docs", unit_id[len("doc:") :]
    return None, unit_id


def _looks_like_doc_section_id(unit_id: str) -> bool:
    """Return True for jDocMunch section IDs.

    jDocMunch section IDs are ``repo::doc_path::slug#level`` where ``level`` is
    numeric. jCodeMunch symbol IDs are usually ``file_path::name#kind`` where
    ``kind`` is textual, so checking the suffix avoids sending symbol lookups
    to the docs store in auto mode.
    """
    head, sep, level = unit_id.rpartition("#")
    return bool(sep and level.isdigit() and head.count("::") >= 2)


def _load_code_index(repo: str, storage_path: Optional[str]):
    from ..storage import IndexStore
    from ._utils import resolve_repo as resolve_code_repo

    owner, name = resolve_code_repo(repo, storage_path)
    return IndexStore(base_path=storage_path).load_index(owner, name)


def _load_doc_index(repo: str, doc_storage_path: Optional[str]):
    from jcodemunch_mcp._jdocmunch.storage import DocStore

    store = DocStore(base_path=doc_storage_path)
    owner, name = store._resolve_repo(repo)
    return store.load_index(owner, name)


def _code_repo_source_root(repo: str, storage_path: Optional[str]) -> str:
    try:
        index = _load_code_index(repo, storage_path)
        return getattr(index, "source_root", "") if index else ""
    except Exception:
        return ""


def _doc_repo_source_root(repo: str, doc_storage_path: Optional[str]) -> str:
    try:
        index = _load_doc_index(repo, doc_storage_path)
        return getattr(index, "source_root", "") if index else ""
    except Exception:
        return ""


def _doc_repo_for_code_repo(
    repo: str,
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> Optional[str]:
    source_root = _code_repo_source_root(repo, storage_path)
    if not source_root:
        return None
    try:
        from jcodemunch_mcp._jdocmunch.storage import DocStore

        for row in DocStore(base_path=doc_storage_path).list_repos():
            if _same_source_root(source_root, row.get("source_root", "")):
                return row.get("repo")
    except Exception:
        return None
    return None


def _code_repo_for_doc_repo(
    repo: str,
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> Optional[str]:
    source_root = _doc_repo_source_root(repo, doc_storage_path)
    if not source_root:
        return None
    try:
        from ..storage import IndexStore

        for row in IndexStore(base_path=storage_path).list_repos():
            if _same_source_root(source_root, row.get("source_root", "")):
                return row.get("repo")
    except Exception:
        return None
    return None


def _resolve_code_repo_for_call(
    repo: str,
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> str:
    if _code_repo_source_root(repo, storage_path):
        return repo
    return _code_repo_for_doc_repo(repo, storage_path, doc_storage_path) or repo


def _resolve_doc_repo_for_call(
    repo: str,
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> str:
    if _doc_repo_source_root(repo, doc_storage_path):
        return repo
    return _doc_repo_for_code_repo(repo, storage_path, doc_storage_path) or repo


def _path_in_code_index(
    repo: str,
    file_path: str,
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> bool:
    try:
        code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_storage_path)
        index = _load_code_index(code_repo, storage_path)
        return bool(index and index.has_source_file(file_path))
    except Exception:
        return False


def _path_in_doc_index(
    repo: str,
    file_path: str,
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> bool:
    try:
        doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_storage_path)
        index = _load_doc_index(doc_repo, doc_storage_path)
        return bool(index and file_path in set(index.doc_paths or []))
    except Exception:
        return False


def _domains_for_path(
    repo: str,
    file_path: str,
    domain: Optional[str],
    storage_path: Optional[str],
    doc_storage_path: Optional[str],
) -> list[str]:
    requested = _normalize_domain(domain)
    if requested == "code":
        return ["code"]
    if requested == "docs":
        return ["docs"]
    if requested == "both":
        return ["code", "docs"]

    hits: list[str] = []
    if _path_in_code_index(repo, file_path, storage_path, doc_storage_path):
        hits.append("code")
    if _path_in_doc_index(repo, file_path, storage_path, doc_storage_path):
        hits.append("docs")
    if hits:
        return hits

    lower = file_path.lower().replace("\\", "/")
    ext = Path(lower).suffix
    if ext in _DOC_STRONG_EXTS or lower.startswith(("docs/", "doc/")):
        return ["docs"]
    if ext in _DOC_AMBIGUOUS_EXTS:
        return ["code", "docs"]
    return ["code"]


def _domains_for_unit(unit_id: str, domain: Optional[str]) -> tuple[list[str], str]:
    requested = _normalize_domain(domain)
    prefix_domain, bare = _strip_unit_prefix(unit_id)
    if requested == "code":
        return ["code"], bare
    if requested == "docs":
        return ["docs"], bare
    if prefix_domain:
        return [prefix_domain], bare
    if requested == "both":
        return ["code", "docs"], bare
    # jDocMunch section ids end with "#<level>"; jCodeMunch symbol ids normally
    # do not. When uncertain, prefer code to avoid reading prose for symbols.
    if _looks_like_doc_section_id(bare):
        return ["docs"], bare
    return ["code"], bare


def _wrap(domain: str, unit_type: str, result: dict) -> dict:
    return {
        "domain": domain,
        "unit_type": unit_type,
        "result": result,
    }


def _response(tool: str, results: list[dict], **extra) -> dict:
    return {
        "tool": tool,
        "results": results,
        "result_count": len(results),
        **extra,
        "_meta": {
            "integrated_docs": True,
            "domains": [r.get("domain") for r in results],
        },
    }


def index_content(
    path: Optional[str] = None,
    url: Optional[str] = None,
    domain: str = "both",
    use_ai_summaries: bool = True,
    use_embeddings: str | bool = "auto",
    incremental: bool = True,
    paths: Optional[list[str]] = None,
    name: Optional[str] = None,
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
    extra_ignore_patterns: Optional[list] = None,
    follow_symlinks: bool = False,
    identity_mode: str = "config",
    progress_cb=None,
) -> dict:
    """Index code, docs, or both from a local path or GitHub URL."""
    requested = _normalize_domain(domain)
    if requested == "auto":
        requested = "both"
    domains = ["code", "docs"] if requested == "both" else [requested]
    if not path and not url:
        return {"error": "index_content requires either path or url"}

    results: list[dict] = []
    doc_store = _doc_storage_path(doc_storage_path)
    doc_name = name
    for d in domains:
        if d == "code":
            if path:
                from .index_folder import index_folder

                result = index_folder(
                    path=path,
                    use_ai_summaries=use_ai_summaries,
                    storage_path=storage_path,
                    extra_ignore_patterns=extra_ignore_patterns,
                    follow_symlinks=follow_symlinks,
                    incremental=incremental,
                    paths=paths,
                    identity_mode=identity_mode,
                    progress_cb=progress_cb,
                )
            else:
                import asyncio as _asyncio

                from .index_repo import index_repo

                result = _asyncio.run(
                    index_repo(
                        url=url or "",
                        use_ai_summaries=use_ai_summaries,
                        storage_path=storage_path,
                        incremental=incremental,
                        extra_ignore_patterns=extra_ignore_patterns,
                    )
                )
            if not doc_name and path and isinstance(result, dict):
                repo_id = result.get("repo", "")
                if isinstance(repo_id, str) and repo_id.startswith("local/"):
                    doc_name = repo_id.split("/", 1)[1]
            results.append(_wrap("code", "symbol", result))
        else:
            if path:
                from jcodemunch_mcp._jdocmunch.tools.index_local import index_local

                result = index_local(
                    path=path,
                    name=doc_name,
                    use_ai_summaries=use_ai_summaries,
                    use_embeddings=use_embeddings,
                    storage_path=doc_store,
                    incremental=incremental,
                    paths=paths,
                )
            else:
                from jcodemunch_mcp._jdocmunch.tools.index_repo import (
                    index_repo as doc_index_repo,
                )

                result = doc_index_repo(
                    url=url or "",
                    use_ai_summaries=use_ai_summaries,
                    use_embeddings=use_embeddings,
                    storage_path=doc_store,
                    incremental=incremental,
                    name=doc_name,
                )
            results.append(_wrap("docs", "section", result))
    return _response("index_content", results)


def list_content(
    repo: str,
    domain: str = "both",
    path_prefix: str = "",
    path_glob: Optional[str] = None,
    tree: bool = False,
    include_summaries: bool = False,
    max_files: Optional[int] = None,
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
) -> dict:
    """List code files and/or documentation files/TOC without content bodies."""
    requested = _normalize_domain(domain)
    domains = ["code", "docs"] if requested in {"auto", "both"} else [requested]
    results: list[dict] = []
    doc_store = _doc_storage_path(doc_storage_path)
    for d in domains:
        if d == "code":
            from .get_file_tree import get_file_tree

            code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_store)
            result = get_file_tree(
                repo=code_repo,
                path_prefix=path_prefix,
                include_summaries=include_summaries,
                max_files=max_files,
                storage_path=storage_path,
            )
            results.append(_wrap("code", "file", result))
        else:
            if tree or path_glob:
                from jcodemunch_mcp._jdocmunch.tools.get_toc_tree import get_toc_tree

                doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
                result = get_toc_tree(
                    repo=doc_repo,
                    path_glob=path_glob,
                    storage_path=doc_store,
                )
            else:
                from jcodemunch_mcp._jdocmunch.tools.list_docs import list_docs

                doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
                result = list_docs(repo=doc_repo, storage_path=doc_store)
            results.append(_wrap("docs", "document", result))
    return _response("list_content", results, repo=repo)


def get_outline(
    repo: str,
    file_path: str = "",
    domain: str = "auto",
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
    file_paths: Optional[list[str]] = None,
) -> dict:
    """Return a code symbol outline or document section outline for a file."""
    doc_store = _doc_storage_path(doc_storage_path)
    domains = _domains_for_path(repo, file_path, domain, storage_path, doc_store)
    results: list[dict] = []
    for d in domains:
        if d == "code":
            from .get_file_outline import get_file_outline

            code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_store)
            result = get_file_outline(
                repo=code_repo,
                file_path=file_path,
                file_paths=file_paths,
                storage_path=storage_path,
            )
            results.append(_wrap("code", "symbol", result))
        else:
            from jcodemunch_mcp._jdocmunch.tools.get_document_outline import (
                get_document_outline,
            )

            doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
            result = get_document_outline(
                repo=doc_repo,
                doc_path=file_path,
                storage_path=doc_store,
            )
            results.append(_wrap("docs", "section", result))
    return _response("get_outline", results, repo=repo, file_path=file_path)


def get_file(
    repo: str,
    file_path: str,
    domain: str = "auto",
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
) -> dict:
    """Return cached code source or cached preprocessed document content."""
    doc_store = _doc_storage_path(doc_storage_path)
    domains = _domains_for_path(repo, file_path, domain, storage_path, doc_store)
    results: list[dict] = []
    for d in domains:
        if d == "code":
            from .get_file_content import get_file_content

            code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_store)
            result = get_file_content(
                repo=code_repo,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                storage_path=storage_path,
            )
            results.append(_wrap("code", "file", result))
        else:
            doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
            result = _get_doc_file_content(
                repo=doc_repo,
                doc_path=file_path,
                start_line=start_line,
                end_line=end_line,
                doc_storage_path=doc_store,
            )
            results.append(_wrap("docs", "document", result))
    return _response("get_file", results, repo=repo, file_path=file_path)


def _get_doc_file_content(
    repo: str,
    doc_path: str,
    start_line: Optional[int],
    end_line: Optional[int],
    doc_storage_path: Optional[str],
) -> dict:
    from jcodemunch_mcp._jdocmunch.storage import DocStore

    store = DocStore(base_path=doc_storage_path)
    try:
        owner, name = store._resolve_repo(repo)
        index = store.load_index(owner, name)
    except Exception as exc:
        return {"error": str(exc)}
    if not index:
        return {"error": f"Repo not found: {repo}"}
    if doc_path not in set(index.doc_paths or []):
        return {"error": f"Document not found in index: {doc_path}"}

    file_path = store._safe_content_path(store._content_dir(owner, name), doc_path)
    if file_path is None or not file_path.exists():
        return {"error": f"Document content not found: {doc_path}"}
    content = file_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    line_count = len(lines)
    if line_count == 0:
        actual_start = actual_end = 0
        selected = ""
    elif start_line is None and end_line is None:
        actual_start = 1
        actual_end = line_count
        selected = content
    else:
        actual_start = max(
            1, min(start_line if start_line is not None else 1, line_count)
        )
        actual_end = max(
            actual_start,
            min(end_line if end_line is not None else line_count, line_count),
        )
        selected = "\n".join(lines[actual_start - 1 : actual_end])

    return {
        "repo": f"{owner}/{name}",
        "file": doc_path,
        "language": "documentation",
        "start_line": actual_start,
        "end_line": actual_end,
        "line_count": line_count,
        "content": selected,
    }


def search_units(
    query: str,
    repo: Optional[str] = None,
    domain: str = "both",
    file_path: Optional[str] = None,
    path_glob: Optional[str] = None,
    max_results: int = 10,
    mode: str = "default",
    kind: Optional[str] = None,
    language: Optional[str] = None,
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
    file_pattern: Optional[str] = None,
    decorator: Optional[str] = None,
    token_budget: Optional[int] = None,
    detail_level: str = "standard",
    debug: bool = False,
    fuzzy: bool = False,
    fuzzy_threshold: float = 0.4,
    max_edit_distance: int = 2,
    sort_by: str = "relevance",
    semantic: bool = False,
    semantic_weight: float = 0.5,
    semantic_only: bool = False,
    fusion: bool = False,
    fqn: Optional[str] = None,
) -> dict:
    """Search symbols and/or documentation sections."""
    requested = _normalize_domain(domain)
    domains = ["code", "docs"] if requested in {"auto", "both"} else [requested]
    results: list[dict] = []
    doc_store = _doc_storage_path(doc_storage_path)
    for d in domains:
        if d == "code":
            if not repo:
                results.append(
                    _wrap(
                        "code", "symbol", {"error": "repo is required for code search"}
                    )
                )
                continue
            from .search_symbols import search_symbols

            code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_store)
            result = search_symbols(
                repo=code_repo,
                query=query,
                kind=kind,
                file_pattern=file_pattern or file_path or path_glob,
                language=language,
                decorator=decorator,
                max_results=max_results,
                token_budget=token_budget,
                detail_level=detail_level,
                debug=debug,
                fuzzy=fuzzy,
                fuzzy_threshold=fuzzy_threshold,
                max_edit_distance=max_edit_distance,
                sort_by=sort_by,
                semantic=semantic,
                semantic_weight=semantic_weight,
                semantic_only=semantic_only,
                fusion=fusion,
                storage_path=storage_path,
                fqn=fqn,
            )
            results.append(_wrap("code", "symbol", result))
        else:
            if mode == "title":
                from jcodemunch_mcp._jdocmunch.tools.search_titles import search_titles

                if not repo:
                    results.append(
                        _wrap(
                            "docs",
                            "section",
                            {"error": "repo is required for title search"},
                        )
                    )
                    continue
                doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
                result = search_titles(
                    repo=doc_repo,
                    query=query,
                    max_results=max_results,
                    storage_path=doc_store,
                )
            else:
                from jcodemunch_mcp._jdocmunch.tools.search_sections import (
                    search_sections,
                )

                doc_repo = (
                    _resolve_doc_repo_for_call(repo, storage_path, doc_store)
                    if repo
                    else None
                )
                result = search_sections(
                    repo=doc_repo,
                    query=query,
                    doc_path=file_path,
                    path_glob=path_glob,
                    max_results=max_results,
                    storage_path=doc_store,
                )
            results.append(_wrap("docs", "section", result))
    return _response("search_units", results, repo=repo, query=query)


def get_unit(
    repo: str,
    unit_id: Optional[str] = None,
    unit_ids: Optional[list[str]] = None,
    domain: str = "auto",
    verify: bool = False,
    context_lines: int = 0,
    strip_boilerplate: bool = False,
    compress_code: bool = False,
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
    verify_against: str = "cache",
    fqn: Optional[str] = None,
    source_start_line: Optional[int] = None,
    source_end_line: Optional[int] = None,
    max_source_lines: Optional[int] = None,
    max_source_bytes: Optional[int] = None,
    max_total_source_bytes: Optional[int] = None,
) -> dict:
    """Retrieve source for a code symbol or content for a doc section."""
    if not unit_id and not unit_ids:
        return {"error": "get_unit requires unit_id or unit_ids"}
    doc_store = _doc_storage_path(doc_storage_path)
    results: list[dict] = []
    ids = unit_ids if unit_ids is not None else [unit_id or ""]
    by_domain: dict[str, list[str]] = {"code": [], "docs": []}
    for raw_id in ids:
        domains, bare = _domains_for_unit(raw_id, domain)
        for d in domains:
            by_domain[d].append(bare)

    if by_domain["code"]:
        from .get_symbol import get_symbol_source

        code_ids = by_domain["code"]
        code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_store)
        result = get_symbol_source(
            repo=code_repo,
            symbol_id=code_ids[0] if len(code_ids) == 1 else None,
            symbol_ids=code_ids if len(code_ids) > 1 else None,
            verify=verify,
            verify_against=verify_against,
            context_lines=context_lines,
            storage_path=storage_path,
            fqn=fqn,
            source_start_line=source_start_line,
            source_end_line=source_end_line,
            max_source_lines=max_source_lines,
            max_source_bytes=max_source_bytes,
            max_total_source_bytes=max_total_source_bytes,
        )
        results.append(_wrap("code", "symbol", result))
    if by_domain["docs"]:
        if len(by_domain["docs"]) == 1:
            from jcodemunch_mcp._jdocmunch.tools.get_section import get_section

            doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
            result = get_section(
                repo=doc_repo,
                section_id=by_domain["docs"][0],
                verify=verify,
                strip_boilerplate=strip_boilerplate,
                compress_code=compress_code,
                storage_path=doc_store,
            )
        else:
            from jcodemunch_mcp._jdocmunch.tools.get_sections import get_sections

            doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
            result = get_sections(
                repo=doc_repo,
                section_ids=by_domain["docs"],
                verify=verify,
                strip_boilerplate=strip_boilerplate,
                compress_code=compress_code,
                storage_path=doc_store,
            )
        results.append(_wrap("docs", "section", result))
    return _response("get_unit", results, repo=repo)


def get_unit_context(
    repo: str,
    unit_id: str,
    domain: str = "auto",
    token_budget: Optional[int] = None,
    include_related: bool = False,
    strip_boilerplate: bool = False,
    storage_path: Optional[str] = None,
    doc_storage_path: Optional[str] = None,
    include_callers: bool = False,
    output_format: str = "json",
    budget_strategy: str = "most_relevant",
    include_budget_report: bool = False,
    fqn: Optional[str] = None,
) -> dict:
    """Retrieve surrounding context for a code symbol or doc section."""
    doc_store = _doc_storage_path(doc_storage_path)
    domains, bare = _domains_for_unit(unit_id, domain)
    results: list[dict] = []
    for d in domains:
        if d == "code":
            from .get_context_bundle import get_context_bundle

            code_repo = _resolve_code_repo_for_call(repo, storage_path, doc_store)
            result = get_context_bundle(
                repo=code_repo,
                symbol_id=bare,
                token_budget=token_budget,
                include_callers=include_callers,
                output_format=output_format,
                budget_strategy=budget_strategy,
                include_budget_report=include_budget_report,
                storage_path=storage_path,
                fqn=fqn,
            )
            results.append(_wrap("code", "symbol", result))
        else:
            from jcodemunch_mcp._jdocmunch.tools.get_section_context import (
                get_section_context,
            )

            doc_repo = _resolve_doc_repo_for_call(repo, storage_path, doc_store)
            result = get_section_context(
                repo=doc_repo,
                section_id=bare,
                max_tokens=token_budget or 2000,
                include_related=include_related,
                strip_boilerplate=strip_boilerplate,
                storage_path=doc_store,
            )
            results.append(_wrap("docs", "section", result))
    return _response("get_unit_context", results, repo=repo)

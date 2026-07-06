"""Integrated jDocMunch reading-surface smoke tests."""

from __future__ import annotations


READING_TOOLS = [
    "index_content",
    "list_content",
    "get_outline",
    "get_file",
    "search_units",
    "get_unit",
    "get_unit_context",
]


def test_reading_surface_exposes_only_unified_tools(monkeypatch):
    from jcodemunch_mcp.server import _build_tools_list

    monkeypatch.setenv("JCODEMUNCH_TOOL_SURFACE", "reading")
    names = [t.name for t in _build_tools_list()]

    assert names == READING_TOOLS
    assert "search_symbols" not in names
    assert "search_sections" not in names


def test_reading_surface_can_be_selected_from_config(monkeypatch):
    from jcodemunch_mcp import config as config_module
    from jcodemunch_mcp.server import _build_tools_list

    monkeypatch.delenv("JCODEMUNCH_TOOL_SURFACE", raising=False)
    monkeypatch.setitem(config_module._GLOBAL_CONFIG, "tool_surface", "jmri")
    names = [t.name for t in _build_tools_list()]

    assert names == READING_TOOLS
    assert "search_symbols" not in names
    assert "search_sections" not in names


def test_index_content_searches_code_and_docs(tmp_path):
    from jcodemunch_mcp.tools.content_router import (
        get_file,
        get_outline,
        get_unit,
        index_content,
        search_units,
    )

    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "app.py").write_text(
        "def hello(name):\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    (docs / "guide.md").write_text(
        "# Guide\n\nUse `hello` to greet people.\n\n## Install\n\nRun it locally.\n",
        encoding="utf-8",
    )

    code_store = str(tmp_path / "code-index")
    doc_store = str(tmp_path / "doc-index")
    indexed = index_content(
        path=str(repo),
        domain="both",
        use_ai_summaries=False,
        use_embeddings=False,
        storage_path=code_store,
        doc_storage_path=doc_store,
    )

    assert indexed["result_count"] == 2
    code_repo = indexed["results"][0]["result"]["repo"]
    doc_repo = indexed["results"][1]["result"]["repo"]
    assert code_repo == doc_repo

    code_search = search_units(
        repo=code_repo,
        query="hello",
        domain="code",
        storage_path=code_store,
    )
    assert code_search["results"][0]["domain"] == "code"
    assert code_search["results"][0]["result"]["results"]

    code_unit_id = code_search["results"][0]["result"]["results"][0]["id"]
    code_unit = get_unit(
        repo=code_repo,
        unit_id=code_unit_id,
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert code_unit["results"][0]["domain"] == "code"
    assert "def hello" in code_unit["results"][0]["result"]["source"]

    doc_search = search_units(
        repo=doc_repo,
        query="greet",
        domain="docs",
        doc_storage_path=doc_store,
    )
    assert doc_search["results"][0]["domain"] == "docs"
    assert doc_search["results"][0]["result"]["results"]

    doc_unit_id = doc_search["results"][0]["result"]["results"][0]["id"]
    doc_unit = get_unit(
        repo=doc_repo,
        unit_id=doc_unit_id,
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert doc_unit["results"][0]["domain"] == "docs"
    assert "Use `hello`" in doc_unit["results"][0]["result"]["section"]["content"]

    outline = get_outline(
        repo=doc_repo,
        file_path="docs/guide.md",
        domain="docs",
        doc_storage_path=doc_store,
    )
    assert outline["results"][0]["unit_type"] == "section"
    assert outline["results"][0]["result"]["section_count"] >= 1

    doc_file = get_file(
        repo=code_repo,
        file_path="docs/guide.md",
        domain="auto",
        start_line=1,
        end_line=1,
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert doc_file["results"][0]["result"]["content"] == "# Guide"

    code_file = get_file(
        repo=doc_repo,
        file_path="app.py",
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert code_file["results"][0]["domain"] == "code"
    assert "def hello" in code_file["results"][0]["result"]["content"]


def test_explicit_doc_name_still_auto_routes_by_source_root(tmp_path):
    from jcodemunch_mcp.tools.content_router import get_file, get_outline, index_content

    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "app.py").write_text(
        "def hello(name):\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    (docs / "guide.md").write_text(
        "# Guide\n\nUse `hello` to greet people.\n",
        encoding="utf-8",
    )

    code_store = str(tmp_path / "code-index")
    doc_store = str(tmp_path / "doc-index")
    indexed = index_content(
        path=str(repo),
        domain="both",
        name="manual-docs",
        use_ai_summaries=False,
        use_embeddings=False,
        storage_path=code_store,
        doc_storage_path=doc_store,
    )

    code_repo = indexed["results"][0]["result"]["repo"]
    doc_repo = indexed["results"][1]["result"]["repo"]
    assert code_repo != doc_repo

    doc_outline = get_outline(
        repo=code_repo,
        file_path="docs/guide.md",
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert doc_outline["results"][0]["domain"] == "docs"
    assert doc_outline["results"][0]["result"]["section_count"] >= 1

    code_file = get_file(
        repo=doc_repo,
        file_path="app.py",
        domain="auto",
        storage_path=code_store,
        doc_storage_path=doc_store,
    )
    assert code_file["results"][0]["domain"] == "code"
    assert "def hello" in code_file["results"][0]["result"]["content"]


def test_doc_store_incremental_save_recomputes_stats_with_kept_content(tmp_path):
    from jcodemunch_mcp._jdocmunch.parser.sections import Section
    from jcodemunch_mcp._jdocmunch.storage.doc_store import DocStore

    repo = "owner/docs"

    def section(path: str, title: str, content: str) -> Section:
        slug = title.lower().replace(" ", "-")
        return Section(
            id=f"{repo}::{path}::{slug}#1",
            repo=repo,
            doc_path=path,
            title=title,
            content=content,
            level=1,
            parent_id="",
            children=[],
            byte_start=0,
            byte_end=len(content.encode("utf-8")),
        )

    store = DocStore(base_path=str(tmp_path / "docs-index"))
    kept_content = "# Kept\n\nretainedbodytoken\n"
    old_changed_content = "# Changed\n\noldbodytoken\n"
    new_changed_content = "# Changed\n\nrevisedbodytoken\n"

    store.save_index(
        "owner",
        "docs",
        [
            section("kept.md", "Kept", kept_content),
            section("changed.md", "Changed", old_changed_content),
        ],
        {
            "kept.md": kept_content,
            "changed.md": old_changed_content,
        },
        {".md": 2},
    )

    updated = store.incremental_save(
        "owner",
        "docs",
        changed_files=["changed.md"],
        new_files=[],
        deleted_files=[],
        new_sections=[section("changed.md", "Changed", new_changed_content)],
        raw_files={"changed.md": new_changed_content},
        doc_types={".md": 2},
    )

    assert updated is not None
    assert updated.bm25_stats["df"]["retainedbodytoken"] == 1
    assert updated.bm25_stats["df"]["revisedbodytoken"] == 1
    assert "oldbodytoken" not in updated.bm25_stats["df"]

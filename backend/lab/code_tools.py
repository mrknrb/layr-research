"""MCP tools for the editor and the flow's palette: the ready-made steps (`blocks`) and help while writing code
(completion, hover, signature, definition, rename, format). All of them are pure: the source in, an answer out;
nothing is saved and the method is never run.

`register(mcp, store, api)` uses nothing from `store` or `api`. At server start the integrator also calls
`codeintel.warm()` (Jedi reads pandas in the background; otherwise the first completion takes seconds).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import codeintel

Source = Annotated[str, Field(description="The method's whole source, as it is in the editor (saved or not).")]
Line = Annotated[int, Field(description="The cursor's line, counted from 1.")]
Column = Annotated[int, Field(description="The cursor's column in that line, counted from 0.")]


def register(mcp, store, api) -> None:
    @mcp.tool()
    def blocks() -> dict[str, Any]:
        """The ready-made steps the flow offers (pick rows, add a column from a formula, summarize by group, join,
        fit a line per group, charts, and the readers that take a file): {blocks: [{function, title, sentence, takes,
        gives, fields: [{name, kind (table, column, columns, text, number, boolean, choice, formula, file, list,
        range), label, default, optional, keyword, of, choices}], example, import, module}], readers: [{function,
        module, label, extensions, out, param}] (the functions that read a raw file: what "add a raw file" offers)}.
        Read from the toolbox's functions (and its add-ons') themselves."""
        from toolbox import registry
        from toolbox.blocks import blocks as palette

        return {"blocks": palette(), "readers": registry.readers()}

    @mcp.tool()
    def code_complete(
        source: Source, line: Line, column: Column,
        columns: Annotated[dict[str, list[str]] | None, Field(description="Columns of tables known from the last "
                                                                         "run, by variable name: {\"cultures\": "
                                                                         "[\"culture\", \"od600\"]}.")] = None,
    ) -> dict[str, Any]:
        """What can be typed at the cursor: {items: [{label, insert, kind, detail, info?, group}], from, prefix,
        note?}. Replace the text from `from` to the cursor with `insert`. Known columns come first after `cultures.`
        and are the only answers inside `cultures["`; then the method's own names, the toolbox, keywords, the rest."""
        return codeintel.complete(source, line, column, columns)

    @mcp.tool()
    def code_hover(
        source: Source, line: Line, column: Column,
        columns: Annotated[dict[str, list[str]] | None, Field(description="Columns of known tables, as for "
                                                                         "code_complete.")] = None,
    ) -> dict[str, Any]:
        """What the name under the cursor is: {name, kind, signature, sentence (the docstring's first line), module,
        line (when it is in this method), type (for a value, e.g. DataFrame)}; {} when there is nothing to say."""
        return codeintel.hover(source, line, column, columns)

    @mcp.tool()
    def code_signature(source: Source, line: Line, column: Column) -> dict[str, Any]:
        """The function being called at the cursor: {signatures: [{name, label, params: [{name, text}], index (the
        input being typed), sentence}]}."""
        return codeintel.signature(source, line, column)

    @mcp.tool()
    def code_definition(source: Source, line: Line, column: Column) -> dict[str, Any]:
        """Where the name under the cursor is defined: {definitions: [{name, kind, in_file, line, column, module,
        sentence, toolbox?}]}. in_file: a line of this method; `toolbox`: a toolbox function (see toolbox_docs)."""
        return codeintel.definition(source, line, column)

    @mcp.tool()
    def code_rename(
        source: Source, line: Line, column: Column,
        new_name: Annotated[str, Field(description="The new name (letters, digits and _; units at the end, e.g. "
                                                   "conc_mM).")],
    ) -> dict[str, Any]:
        """Rename the name under the cursor everywhere in the method: {source (the whole new file), old_name,
        new_name, lines (that changed), warning?}. Nothing is saved; save with method_write."""
        return codeintel.rename(source, line, column, new_name)

    @mcp.tool()
    def code_format(source: Source) -> dict[str, Any]:
        """The method laid out tidily by Ruff (lines up to 120 characters): {source, changed, error?}. Only when
        asked; nothing is saved."""
        return codeintel.format_source(source)

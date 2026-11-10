"""MCP queries with immutable analysis scopes and bounded graph responses."""

from callmap.contexts import AnalysisContexts
from callmap.impact import impact
from callmap.projects import ProjectStores
from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


def create_server(store: GraphStore | ProjectStores | None = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import TextContent

    projects = store if isinstance(store, ProjectStores) else ProjectStores(store)
    contexts = AnalysisContexts(projects)
    server = FastMCP(
        "callmap",
        instructions=(
            "Start with absolute project_path or reuse analysis_context. "
            "Scopes bind scan options, source, environment, annotations and Git. "
            "Changes expire explicitly. context_entry narrows static propagation. "
            "Use search_functions filters and find_references for definitions/reads. "
            "explanation shows factory guards and evidence gaps. Follow continuation "
            "for response pages and recovery.next_query for missing evidence. "
            "impact_of_changes links diff symbols to static entries/tests. "
            "Static relations never prove runtime calls, deployment or test coverage."
        ),
    )

    def resolve(
        project_path,
        excludes,
        exclude_tests,
        scan_mode,
        context_entry,
        analysis_context,
    ):
        return contexts.resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )

    def bind(selected, spec, expected, data):
        token = contexts.bind(selected, spec, data, expected)
        data["analysis_context"] = token
        contexts.scope_queries(data, token)
        return data

    @server.tool()
    def open_analysis_context(
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        context_entry: str | None = None,
        analysis_context: str | None = None,
    ) -> dict:
        """Create/validate an immutable query scope without executing project code."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        graph = selected._query_graph(entry)
        data = {
            "snapshot": graph["snapshot"],
            "analysis_fingerprint": graph["analysis_fingerprint"],
        }
        bind(selected, spec, expected, data)
        data["scope"] = contexts.describe(data["analysis_context"])
        return data

    @server.tool()
    def search_functions(
        query: str = "",
        limit: int = 50,
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        analysis_context: str | None = None,
        context_entry: str | None = None,
        symbol_type: str | None = None,
        path: str | None = None,
        decorator: str | None = None,
        route: str | None = None,
        owner: str | None = None,
        entry_kind: str | None = None,
    ) -> dict:
        """Search symbols, owners, paths, decorators/routes and distinct entry kinds."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        graph = selected._query_graph(entry)
        result = selected.search(
            query,
            limit,
            symbol_type=symbol_type,
            path=path,
            decorator=decorator,
            route=route,
            owner=owner,
            entry_kind=entry_kind,
            _graph=graph,
        )
        return bind(
            selected,
            spec,
            expected,
            {
                "result": result,
                "snapshot": graph["snapshot"],
                "analysis_fingerprint": graph["analysis_fingerprint"],
            },
        )

    @server.tool()
    def find_references(
        query: str,
        project_path: str | None = None,
        path: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        context_entry: str | None = None,
        analysis_context: str | None = None,
    ) -> dict:
        """Locate field definitions, reads and override writes."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        return bind(
            selected,
            spec,
            expected,
            selected.references(
                query,
                path=path,
                kind=kind,
                limit=limit,
                offset=offset,
                context_entry=entry,
            ),
        )

    @server.tool()
    def find_entry_points(
        project_path: str | None = None,
        limit: int = 50,
        offset: int = 0,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        analysis_context: str | None = None,
        context_entry: str | None = None,
    ) -> dict:
        """Discover distinct static entry kinds; no deployment proof."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        return bind(selected, spec, expected, selected.entries(limit, offset))

    def graph_result(
        direction,
        symbol,
        depth,
        max_nodes,
        project_path,
        compact,
        include_docs,
        prefer_internal,
        internal_only,
        budget,
        cursor,
        snapshot,
        context_entry,
        excludes,
        exclude_tests,
        scan_mode,
        analysis_context,
    ):
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        graph = selected.expand(
            symbol,
            depth,
            max_nodes,
            context_entry=entry,
            prefer_internal=prefer_internal,
            internal_only=internal_only,
            direction=direction,
        )
        bind(selected, spec, expected, graph)
        query = {
            "symbol": symbol,
            "depth": depth,
            "max_nodes": max_nodes,
            "project_path": str(selected.root),
            "compact": compact,
            "include_docs": include_docs,
            "prefer_internal": prefer_internal,
            "internal_only": internal_only,
            "budget": budget,
            "context_entry": entry,
            "excludes": spec["excludes"],
            "exclude_tests": spec["exclude_tests"],
            "scan_mode": spec["scan_mode"],
            "analysis_context": graph["analysis_context"],
        }
        payload = bounded_graph(
            graph,
            {
                k: v
                for k, v in query.items()
                if k
                not in {
                    "project_path",
                    "excludes",
                    "exclude_tests",
                    "scan_mode",
                    "context_entry",
                }
            },
            budget=budget,
            compact=compact,
            include_docs=include_docs,
            cursor=cursor,
            snapshot=snapshot,
        )
        if len(serialize(payload)) > budget:
            raise ValueError(
                "budget insufficient for analysis context; increase budget"
            )
        return [TextContent(type="text", text=serialize(payload))]

    @server.tool(structured_output=False)
    def expand_calls(
        symbol: str,
        depth: int = 3,
        max_nodes: int = 200,
        project_path: str | None = None,
        compact: bool = True,
        include_docs: bool = False,
        prefer_internal: bool = True,
        internal_only: bool = False,
        budget: int = 20000,
        cursor: int = 0,
        snapshot: str | None = None,
        context_entry: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        analysis_context: str | None = None,
    ) -> list[TextContent]:
        """Expand guarded calls and explanations within a character budget."""
        return graph_result(
            "downstream",
            symbol,
            depth,
            max_nodes,
            project_path,
            compact,
            include_docs,
            prefer_internal,
            internal_only,
            budget,
            cursor,
            snapshot,
            context_entry,
            excludes,
            exclude_tests,
            scan_mode,
            analysis_context,
        )

    @server.tool(structured_output=False)
    def find_callers(
        symbol: str,
        depth: int = 1,
        max_nodes: int = 200,
        project_path: str | None = None,
        compact: bool = True,
        include_docs: bool = False,
        prefer_internal: bool = True,
        internal_only: bool = False,
        budget: int = 20000,
        cursor: int = 0,
        snapshot: str | None = None,
        context_entry: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        analysis_context: str | None = None,
    ) -> list[TextContent]:
        """Trace static callers; pagination does not prove runtime coverage."""
        return graph_result(
            "upstream",
            symbol,
            depth,
            max_nodes,
            project_path,
            compact,
            include_docs,
            prefer_internal,
            internal_only,
            budget,
            cursor,
            snapshot,
            context_entry,
            excludes,
            exclude_tests,
            scan_mode,
            analysis_context,
        )

    @server.tool()
    def read_symbol(
        symbol: str,
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        context_entry: str | None = None,
        analysis_context: str | None = None,
    ) -> dict:
        """Read source/docs using the same immutable analysis scope."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        return bind(selected, spec, expected, selected.read_node(symbol, entry))

    @server.tool()
    def get_context(
        symbol: str,
        budget: int = 12000,
        depth: int = 3,
        include_code: bool = False,
        project_path: str | None = None,
        context_entry: str | None = None,
        include_external_docs: bool = False,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        direction: str = "downstream",
        analysis_context: str | None = None,
    ) -> dict:
        """Return bounded Markdown plus its immutable scope identifier."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        graph = selected.expand(
            symbol,
            depth,
            include_code=include_code,
            context_entry=entry,
            direction=direction,
            prefer_internal=True,
        )
        text = selected.context(
            symbol,
            budget,
            depth,
            include_code,
            context_entry=entry,
            include_external_docs=include_external_docs,
            direction=direction,
            _graph=graph,
        )
        return bind(
            selected,
            spec,
            expected,
            {
                "text": text,
                "snapshot": graph["snapshot"],
                "analysis_fingerprint": graph["analysis_fingerprint"],
            },
        )

    @server.tool()
    def index_status(
        project_path: str | None = None,
        force: bool = False,
        file_offset: int = 0,
        file_limit: int = 100,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        analysis_context: str | None = None,
        context_entry: str | None = None,
    ) -> dict:
        """Audit indexed/skipped files; start or validate an analysis scope."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        return bind(
            selected, spec, expected, selected.stats(force, file_offset, file_limit)
        )

    @server.tool()
    def annotate_symbol(
        symbol: str,
        doc: str,
        targets: list[str] | None = None,
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        analysis_context: str | None = None,
        context_entry: str | None = None,
    ) -> dict:
        """Save explicit annotations, rotating the context after the change."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        if expected:
            graph = selected._query_graph(entry)
            contexts.bind(selected, spec, graph, expected)
        result = selected.annotate(
            symbol,
            doc,
            targets,
            context_entry=entry,
            expected_fingerprint=expected["analysis_fingerprint"] if expected else None,
        )
        graph = selected._query_graph(entry)
        return bind(
            selected,
            spec,
            None,
            result
            | {
                "snapshot": graph["snapshot"],
                "analysis_fingerprint": graph["analysis_fingerprint"],
                "previous_analysis_context": analysis_context,
            },
        )

    @server.tool(structured_output=False)
    def impact_of_changes(
        project_path: str | None = None,
        base: str = "HEAD",
        patch: str | None = None,
        limit: int = 20,
        offset: int = 0,
        max_nodes: int = 200,
        budget: int = 20000,
        excludes: list[str] | None = None,
        exclude_tests: bool | None = None,
        scan_mode: str | None = None,
        context_entry: str | None = None,
        analysis_context: str | None = None,
    ) -> list[TextContent]:
        """Link diff symbols to baseline/current static entries and tests."""
        selected, entry, spec, expected = resolve(
            project_path,
            excludes,
            exclude_tests,
            scan_mode,
            context_entry,
            analysis_context,
        )
        data = bind(
            selected,
            spec,
            expected,
            impact(
                selected, base, patch, limit, offset, max_nodes, entry, budget - 256
            ),
        )
        data["view"] = "compact"
        return [TextContent(type="text", text=serialize(data))]

    return server


def run(store: GraphStore | ProjectStores | None = None) -> None:
    create_server(store).run(transport="stdio")

"""Agent access through the official MCP Python SDK."""

from callmap.projects import ProjectStores
from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


def create_server(store: GraphStore | ProjectStores | None = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import TextContent

    projects = store if isinstance(store, ProjectStores) else ProjectStores(store)
    server = FastMCP(
        "callmap",
        instructions=(
            "Analyze Python projects using explicit absolute project_path on every "
            "tool call. Start with index_status, search_functions and expand_calls, "
            "then read_symbol for relevant source. Check truncation and evidence; "
            "Use find_callers to trace upstream callers (default depth=1). "
            "Use find_entry_points to discover startup targets and candidate roots. "
            "expand_calls defaults to compact, bounded JSON without full docs; "
            "follow continuation for response pagination, distinct from graph limits. "
            "Pass context_entry to narrow argument candidates to an entry; "
            "exclude_tests/excludes/scan_mode must be repeated on each query. "
            "Use index_status coverage to audit indexed/skipped files. "
            "candidate edges are possibilities, not runtime facts. Never invent "
            "annotation targets. A configured default project is used only when "
            "project_path is omitted."
        ),
    )

    @server.tool()
    def search_functions(
        query: str,
        limit: int = 50,
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> list[dict]:
        """Search Python symbols in project_path (absolute workspace directory).

        Returns unique IDs and source locations. Omit project_path only when a
        default project was explicitly configured at server startup.
        """
        return projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        ).search(query, limit)

    @server.tool()
    def find_entry_points(
        project_path: str | None = None,
        limit: int = 50,
        offset: int = 0,
        excludes: list[str] | None = None,
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> dict:
        """Discover entry points without running project code.

        Explicit targets come from project scripts, __main__.py or main guards.
        Recognized framework registrations and public functions without known
        internal callers are candidates, not guaranteed runtime entry points.
        Reasons explain each result; auto_entry is set only for a single explicit
        target. Follow next_offset for pagination and repeat scan filters.
        """
        return projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        ).entries(limit, offset)

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
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> list[TextContent]:
        """Expand compact calls in project_path; default omits full docs and source.

        depth=-1 expands all reachable nodes, subject to max_nodes; truncation
        graph_truncated is a depth/node limit; response_truncated is JSON pagination.
        budget is the exact CHARACTER limit of the returned JSON text (minimum 2000).
        Follow continuation.arguments unchanged to fetch the next page. read_symbol
        fetches full docs/source. context_entry narrows static argument propagation
        to reachable callers, not a runtime trace. repeat scan filters on every call.
        """
        selected = projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        )
        graph = selected.expand(
            symbol,
            depth,
            max_nodes,
            context_entry=context_entry,
            prefer_internal=prefer_internal,
            internal_only=internal_only,
        )
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
            "context_entry": context_entry,
            "excludes": excludes,
            "exclude_tests": exclude_tests,
            "scan_mode": scan_mode,
        }
        payload = bounded_graph(
            graph,
            query,
            budget=budget,
            compact=compact,
            include_docs=include_docs,
            cursor=cursor,
            snapshot=snapshot,
        )
        return [TextContent(type="text", text=serialize(payload))]

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
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> list[TextContent]:
        """Find upstream callers in project_path; default depth=1 is direct callers.

        depth=-1 traces all reachable callers, subject to max_nodes. Edges keep
        source=caller, target=callee with original callsite/status/evidence,
        including synthetic constructor/callback relationships. Node depth is
        distance upstream; incoming_count includes relationships beyond this view.
        Candidates are possibilities, not runtime facts. context_entry narrows
        argument inference, not a runtime trace. Repeat scan filters on each call.
        budget is a CHARACTER limit (minimum 2000). Follow continuation.arguments
        with find_callers for pagination; graph_truncated is a separate graph limit.
        """
        selected = projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        )
        graph = selected.callers(
            symbol,
            depth,
            max_nodes,
            context_entry=context_entry,
            prefer_internal=prefer_internal,
            internal_only=internal_only,
        )
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
            "context_entry": context_entry,
            "excludes": excludes,
            "exclude_tests": exclude_tests,
            "scan_mode": scan_mode,
        }
        payload = bounded_graph(
            graph,
            query,
            budget=budget,
            compact=compact,
            include_docs=include_docs,
            cursor=cursor,
            snapshot=snapshot,
        )
        return [TextContent(type="text", text=serialize(payload))]

    @server.tool()
    def read_symbol(
        symbol: str,
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool = False,
        scan_mode: str | None = None,
        context_entry: str | None = None,
    ) -> dict:
        """Read source and docs in project_path (absolute workspace directory)."""
        selected = projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        )
        return selected.select(selected.graph(context_entry), symbol)

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
        exclude_tests: bool = False,
        scan_mode: str | None = None,
        direction: str = "downstream",
    ) -> str:
        """Get context within a CHARACTER budget; direction can be upstream."""
        return projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        ).context(
            symbol,
            budget,
            depth,
            include_code,
            context_entry=context_entry,
            include_external_docs=include_external_docs,
            direction=direction,
        )

    @server.tool()
    def index_status(
        project_path: str | None = None,
        force: bool = False,
        file_offset: int = 0,
        file_limit: int = 100,
        excludes: list[str] | None = None,
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> dict:
        """Refresh project_path (absolute path); report errors, counts and cache.

        coverage lists indexed/skipped files and directories, reasons and config.
        Follow coverage.next_offset using file_offset to page file lists. No parse
        errors means only indexed files parsed successfully, not full workspace
        coverage. scan_mode='filesystem' also scans nested repositories/Git-ignored
        Python files, retaining builtin and configured excludes.
        """
        return projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        ).stats(force, file_offset, file_limit)

    @server.tool()
    def annotate_symbol(
        symbol: str,
        doc: str,
        targets: list[str] | None = None,
        project_path: str | None = None,
        excludes: list[str] | None = None,
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> dict:
        """Save supplied docs/targets in project_path (absolute workspace path).

        Do not use this to invent resolved calls. Stale overrides stop applying.
        """
        return projects.get(
            project_path,
            excludes=excludes,
            exclude_tests=exclude_tests,
            scan_mode=scan_mode,
        ).annotate(symbol, doc, targets)

    return server


def run(store: GraphStore | ProjectStores | None = None) -> None:
    create_server(store).run(transport="stdio")

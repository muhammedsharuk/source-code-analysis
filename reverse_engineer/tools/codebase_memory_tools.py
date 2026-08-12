"""Deep Agents tools wrapping CodebaseMemoryCLI.

Pass CODEBASE_MEMORY_TOOLS to create_deep_agent(tools=...).
See https://docs.langchain.com/oss/python/deepagents/tools
"""

from typing import Any, Literal

from utils.codebase_memory import CodebaseMemoryCLI

_cli = CodebaseMemoryCLI()

def index_repository(
    repo_path: str,
    mode: str = "full",
    target_projects: list[str] | None = None,
    name: str | None = None,
    persistence: bool = False,
):
    """Index a repository into the knowledge graph."""
    return _cli.index_repository(repo_path=repo_path, mode=mode, target_projects=target_projects, name=name, persistence=persistence)

# def delete_project(
#     project: str,
# ):
#     """Delete a project from the knowledge graph."""
#     return _cli.delete_project(project=project)

def index_status(
    project: str,
):
    """Get indexing status for a project."""
    return _cli.index_status(project=project)

def check_index_coverage(
    project: str,
    paths: list[str] | None = None,
    scopes: list[str] | None = None,
    scope_limit: int = 200,
    scope_offset: int = 0,
):
    """Check indexing coverage for a project."""
    return _cli.check_index_coverage(project=project, paths=paths, scopes=scopes, scope_limit=scope_limit, scope_offset=scope_offset)

def list_projects() -> Any:
    """List all indexed codebase-memory projects and their root paths."""
    return _cli.list_projects()


def get_architecture(
    project: str,
    path: str | None = None,
    aspects: list[str] | None = None,
) -> Any:
    """Get architectural overview of a project.

    Args:
        project: Project name as returned by list_projects.
        path: Optional subdirectory to scope the architecture view.
        aspects: Optional list of aspects to include (e.g. layers, modules).
    """
    return _cli.get_architecture(project=project, path=path, aspects=aspects)


def search_graph(
    project: str,
    query: str | None = None,
    label: str | None = None,
    name_pattern: str | None = None,
    qn_pattern: str | None = None,
    file_pattern: str | None = None,
    relationship: str | None = None,
    min_degree: int | None = None,
    max_degree: int | None = None,
    exclude_entry_points: bool | None = None,
    include_connected: bool | None = None,
    semantic_query: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    format: str | None = None,
    fields: list[str] | None = None,
) -> Any:
    """Search the code knowledge graph for symbols, relationships, and patterns.

    Args:
        project: Project name as returned by list_projects.
        query: Free-text graph search query.
        label: Node label filter (e.g. Function, Class, File).
        name_pattern: Glob/regex-style filter on symbol names.
        qn_pattern: Filter on qualified names.
        file_pattern: Filter nodes by source file path pattern.
        relationship: Edge/relationship type to filter on.
        min_degree: Minimum node degree.
        max_degree: Maximum node degree.
        exclude_entry_points: When True, skip entry-point nodes.
        include_connected: When True, include neighboring connected nodes.
        semantic_query: List of semantic search phrases.
        limit: Max results to return (default 50).
        offset: Pagination offset.
        format: Optional output format override.
        fields: Optional list of fields to include in each result.
    """
    return _cli.search_graph(
        project=project,
        query=query,
        label=label,
        name_pattern=name_pattern,
        qn_pattern=qn_pattern,
        file_pattern=file_pattern,
        relationship=relationship,
        min_degree=min_degree,
        max_degree=max_degree,
        exclude_entry_points=exclude_entry_points,
        include_connected=include_connected,
        semantic_query=semantic_query,
        limit=limit,
        offset=offset,
        format=format,
        fields=fields,
    )


def trace_path(
    project: str,
    function_name: str,
    direction: Literal["upstream", "downstream", "both"] = "both",
    depth: int = 3,
    mode: Literal["calls", "dataflow", "both"] = "calls",
    parameter_name: str | None = None,
    edge_types: list[str] | None = None,
    risk_labels: bool = False,
    include_tests: bool = False,
    format: str | None = None,
) -> Any:
    """Trace call or dataflow paths for a function through the code graph.

    Args:
        project: Project name as returned by list_projects.
        function_name: Function name or qualified name to start from.
        direction: Trace upstream callers, downstream callees, or both.
        depth: How many hops to traverse (default 3).
        mode: Trace call edges, dataflow edges, or both.
        parameter_name: Optional parameter to focus dataflow tracing on.
        edge_types: Optional list of edge types to include.
        risk_labels: When True, annotate results with risk labels.
        include_tests: When True, include test code in the trace.
        format: Optional output format override.
    """
    return _cli.trace_path(
        project=project,
        function_name=function_name,
        direction=direction,
        depth=depth,
        mode=mode,
        parameter_name=parameter_name,
        edge_types=edge_types,
        risk_labels=risk_labels,
        include_tests=include_tests,
        format=format,
    )


def query_graph(
    project: str,
    query: str,
    graph: str = "code",
    max_rows: int | None = None,
) -> Any:
    """Run a raw query against a project graph.

    Args:
        project: Project name as returned by list_projects.
        query: Graph query string.
        graph: Which graph to query (default "code").
        max_rows: Optional cap on returned rows.
    """
    return _cli.query_graph(
        project=project,
        query=query,
        graph=graph,
        max_rows=max_rows,
    )


def get_graph_schema(project: str) -> Any:
    """Get the schema (node labels, edge types, properties) for a project graph.

    Args:
        project: Project name as returned by list_projects.
    """
    return _cli.get_graph_schema(project=project)


def get_code_snippet(
    project: str,
    qualified_name: str,
    include_neighbors: bool = False,
) -> Any:
    """Fetch the source snippet for a symbol by qualified name.

    Args:
        project: Project name as returned by list_projects.
        qualified_name: Fully qualified symbol name.
        include_neighbors: When True, also return neighboring related snippets.
    """
    return _cli.get_code_snippet(
        project=project,
        qualified_name=qualified_name,
        include_neighbors=include_neighbors,
    )


def search_code(
    project: str,
    pattern: str,
    file_pattern: str | None = None,
    path_filter: str | None = None,
    mode: Literal["compact", "content", "files_with_matches"] = "compact",
    context: int | None = None,
    regex: bool = False,
    limit: int = 10,
) -> Any:
    """Search source files for a text or regex pattern.

    Args:
        project: Project name as returned by list_projects.
        pattern: Text or regex pattern to search for.
        file_pattern: Optional glob for file names (e.g. "*.py").
        path_filter: Optional path prefix/subdirectory filter.
        mode: Result mode — compact, content, or files_with_matches.
        context: Optional number of surrounding context lines.
        regex: When True, treat pattern as a regular expression.
        limit: Max matches to return (default 10).
    """
    return _cli.search_code(
        project=project,
        pattern=pattern,
        file_pattern=file_pattern,
        path_filter=path_filter,
        mode=mode,
        context=context,
        regex=regex,
        limit=limit,
    )


def detect_changes(
    project: str,
    scope: str | None = None,
    depth: int = 2,
    base_branch: str = "main",
    since: str | None = None,
) -> Any:
    """Detect code changes and their blast radius in the graph.

    Args:
        project: Project name as returned by list_projects.
        scope: Optional path or symbol scope to analyze.
        depth: How deep to expand impact through the graph (default 2).
        base_branch: Git branch to diff against (default "main").
        since: Optional time/commit bound for change detection.
    """
    return _cli.detect_changes(
        project=project,
        scope=scope,
        depth=depth,
        base_branch=base_branch,
        since=since,
    )


def manage_adr(
    project: str,
    mode: Literal["get", "set", "update"] = "get",
    content: str | None = None,
    sections: list[str] | None = None,
) -> Any:
    """Get or update Architecture Decision Records (ADRs) for a project.

    Args:
        project: Project name as returned by list_projects.
        mode: Operation mode — get, set, or update.
        content: ADR content when setting or updating.
        sections: Optional list of ADR sections to read or write.
    """
    return _cli.manage_adr(
        project=project,
        mode=mode,
        content=content,
        sections=sections,
    )


def ingest_traces(
    project: str,
    traces: list[dict],
) -> Any:
    """Ingest runtime/execution traces into the project knowledge graph.

    Args:
        project: Project name as returned by list_projects.
        traces: List of trace dictionaries to ingest.
    """
    return _cli.ingest_traces(project=project, traces=traces)


CODEBASE_MEMORY_TOOLS = [
    index_repository,
    # delete_project,
    index_status,
    check_index_coverage,
    list_projects,
    get_architecture,
    search_graph,
    trace_path,
    query_graph,
    get_graph_schema,
    get_code_snippet,
    search_code,
    detect_changes,
    manage_adr,
    ingest_traces,
]

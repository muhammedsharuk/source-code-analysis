import json
import subprocess
import time

class CodebaseMemoryCLI:
    def __init__(
        self,
        executable: str = "codebase-memory-mcp",
        max_retries: int = 2,
        retry_delay_seconds: float = 1.5,
    ):
        self.executable = executable
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds

    def _run(self, command: str, **options):
        payload = {
            k: v
            for k, v in options.items()
            if v is not None
        }

        cmd = [
            self.executable,
            "cli",
            command,
            json.dumps(payload),
            "--json",
        ]

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 2):
            print("Running:", cmd, f"(attempt {attempt})" if attempt > 1 else "")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"'{command}' failed with exit code {result.returncode}: "
                    f"{(result.stderr or '').strip() or '(no stderr output)'}"
                )

            stdout = (result.stdout or "").strip()

            if not stdout:
                last_error = RuntimeError(
                    f"'{command}' exited successfully but produced no output. "
                    f"stderr: {(result.stderr or '').strip() or '(none)'}. "
                    "This is usually a transient issue (e.g. concurrent access to the "
                    "knowledge graph store) rather than a real failure."
                )
            else:
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError as exc:
                    last_error = RuntimeError(
                        f"'{command}' returned output that is not valid JSON ({exc}). "
                        f"Raw output (truncated): {stdout[:500]!r}"
                    )

            if attempt <= self.max_retries:
                time.sleep(self.retry_delay_seconds)

        assert last_error is not None
        raise last_error

    def index_repository(
        self,
        repo_path: str,
        mode: str = "full",
        target_projects: list[str] | None = None,
        name: str | None = None,
        persistence: bool = False,
    ):
        """
        Index a repository into the knowledge graph.
        """

        return self._run(
            "index_repository",
            repo_path=repo_path,
            mode=mode,
            target_projects=target_projects,
            name=name,
            persistence=persistence,
        )
    def delete_project(
        self,
        project: str,
    ):
        """
        Delete an indexed project.
        """

        return self._run(
            "delete_project",
            project=project,
        )


    def index_status(
        self,
        project: str,
    ):
        """
        Get indexing status for a project.
        """

        return self._run(
            "index_status",
            project=project,
        )


    def list_projects(self):
        return self._run("list_projects")


    def get_architecture(
        self,
        project: str,
        path: str | None = None,
        aspects: list[str] | None = None,
    ):
        return self._run(
            "get_architecture",
            project=project,
            path=path,
            aspects=json.dumps(aspects) if aspects else None,
        )


    def search_graph(
        self,
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
    ):
        return self._run(
            "search_graph",
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
            semantic_query=json.dumps(semantic_query) if semantic_query else None,
            limit=limit,
            offset=offset,
            format=format,
            fields=json.dumps(fields) if fields else None,
        )


    def trace_path(
        self,
        project: str,
        function_name: str,
        direction: str = "both",
        depth: int = 3,
        mode: str = "calls",
        parameter_name: str | None = None,
        edge_types: list[str] | None = None,
        risk_labels: bool = False,
        include_tests: bool = False,
        format: str | None = None,
    ):
        return self._run(
            "trace_path",
            project=project,
            function_name=function_name,
            direction=direction,
            depth=depth,
            mode=mode,
            parameter_name=parameter_name,
            edge_types=json.dumps(edge_types) if edge_types else None,
            risk_labels=risk_labels,
            include_tests=include_tests,
            format=format,
        )


    def query_graph(
        self,
        project: str,
        query: str,
        graph: str = "code",
        max_rows: int | None = None,
    ):
        return self._run(
            "query_graph",
            project=project,
            query=query,
            graph=graph,
            max_rows=max_rows,
        )


    def get_graph_schema(self, project: str):
        return self._run(
            "get_graph_schema",
            project=project,
        )


    def get_code_snippet(
        self,
        project: str,
        qualified_name: str,
        include_neighbors: bool = False,
    ):
        return self._run(
            "get_code_snippet",
            project=project,
            qualified_name=qualified_name,
            include_neighbors=include_neighbors,
        )


    def search_code(
        self,
        project: str,
        pattern: str,
        file_pattern: str | None = None,
        path_filter: str | None = None,
        mode: str = "compact",
        context: int | None = None,
        regex: bool = False,
        limit: int = 10,
    ):
        return self._run(
            "search_code",
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
        self,
        project: str,
        scope: str | None = None,
        depth: int = 2,
        base_branch: str = "main",
        since: str | None = None,
    ):
        return self._run(
            "detect_changes",
            project=project,
            scope=scope,
            depth=depth,
            base_branch=base_branch,
            since=since,
        )


    def manage_adr(
        self,
        project: str,
        mode: str = "get",
        content: str | None = None,
        sections: list[str] | None = None,
    ):
        return self._run(
            "manage_adr",
            project=project,
            mode=mode,
            content=content,
            sections=json.dumps(sections) if sections else None,
        )


    def ingest_traces(
        self,
        project: str,
        traces: list[dict],
    ):
        return self._run(
            "ingest_traces",
            project=project,
            traces=json.dumps(traces),
        )
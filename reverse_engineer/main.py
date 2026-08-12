from agents.orchestrator import create_orchestrator_agent
from config import repo_path

if __name__ == "__main__":
    if not repo_path:
        raise SystemExit("REPO_PATH is not set. Add it to your .env file.")

    orchestrator = create_orchestrator_agent(repo_path)

    request = (
        f"Index the repository at path '{repo_path}', "
        "then discover the codebase and the features of the project."
    )

    orchestrator.invoke({"messages": [{"role": "user", "content": request}]})

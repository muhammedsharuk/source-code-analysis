from dotenv import load_dotenv
import os
from pathlib import Path

load_dotenv()

repo_path = os.getenv("REPO_PATH")
compact_llm_model = os.getenv("COMPACT_LLM_MODEL")
standard_llm_model = os.getenv("STANDARD_LLM_MODEL")
large_llm_model = os.getenv("LARGE_LLM_MODEL")


default_output_dir = Path(__file__).resolve().parent / "outputs"
output_dir = Path(os.getenv("OUTPUT_DIR", str(default_output_dir))).expanduser().resolve()
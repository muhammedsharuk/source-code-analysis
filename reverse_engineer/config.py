from dotenv import load_dotenv
import os
from pathlib import Path
from langchain.chat_models import init_chat_model

load_dotenv()

def create_chat_model(model_name: str):
    kwargs = {"model": model_name, "timeout": 1200, "max_retries": 2}
    if "terra" in  model_name.lower():
        kwargs["model_kwargs"] = {"reasoning_effort": "none"}
    return init_chat_model(**kwargs)
    

repo_path = os.getenv("REPO_PATH")
compact_llm_model = create_chat_model(os.getenv("COMPACT_LLM_MODEL"))
standard_llm_model = create_chat_model(os.getenv("STANDARD_LLM_MODEL"))
large_llm_model = create_chat_model(os.getenv("LARGE_LLM_MODEL"))


default_output_dir = Path(__file__).resolve().parent / "outputs"
output_dir = Path(os.getenv("OUTPUT_DIR", str(default_output_dir))).expanduser().resolve()
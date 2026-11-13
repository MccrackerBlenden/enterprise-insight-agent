"""Model configuration; no network calls or heavy imports."""
import os
from dotenv import load_dotenv

load_dotenv()

def is_ready() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip() and
                os.getenv("OPENAI_MODEL", "").strip())

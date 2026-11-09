import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PORT = int(os.getenv("PORT", "5000"))
CONTEXT_WINDOW = int(os.getenv("CONTEXT_WINDOW", "4096"))
TIMEOUT = int(os.getenv("MODEL_TIMEOUT", "300"))


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class Generation:
    text: str
    truncated: bool = False


class ModelProvider(Protocol):
    def generate(self, spec, messages, schema=None) -> Generation:
        ...


def load_prompt(filename, **values):
    try:
        text = (BASE_DIR / "prompts" / filename).read_text(
            encoding="utf-8"
        ).strip()
        if not text:
            raise ValueError("Empty prompt")
        return text.format(**values) if values else text
    except (OSError, KeyError, ValueError) as exc:
        raise AppError(f"Invalid prompt file: {filename}", 500) from exc


def validate_prompts():
    for filename in [
        "chat_system.txt",
        "continue_project.txt",
        "extract_request.txt",
        "extract_retry.txt",
        "extract_system.txt",
        "handoff_reference.txt",
    ]:
        load_prompt(filename)


class OllamaProvider:
    def generate(self, spec, messages, schema=None):
        estimated = sum(
            len(item["content"].encode("utf-8")) // 3 + 32
            for item in messages
        )
        if estimated + spec["max_output"] + 128 > CONTEXT_WINDOW:
            raise AppError(
                "Estimated context limit reached. Shorten the conversation "
                "or increase CONTEXT_WINDOW if memory permits."
            )

        keep_alive = os.getenv("KEEP_ALIVE", "0")
        payload = {
            "model": spec["model"],
            "messages": messages,
            "stream": False,
            "keep_alive": 0 if keep_alive == "0" else keep_alive,
            "options": {
                "num_ctx": CONTEXT_WINDOW,
                "num_predict": spec["max_output"],
                "temperature": 0 if schema is not None else 0.3,
            },
        }
        if schema is not None:
            payload["format"] = schema

        try:
            response = requests.post(
                f"{spec['endpoint']}/api/chat",
                json=payload,
                timeout=TIMEOUT,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AppError("Ollama unavailable or invalid response.", 502) from exc

        if not isinstance(data, dict):
            raise AppError("Unexpected Ollama response.", 502)
        if not response.ok:
            raise AppError(
                f"Ollama: {str(data.get('error', 'Request failed'))[:400]}",
                502,
            )

        message = data.get("message", {})
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise AppError("Ollama returned no text.", 502)

        return Generation(text.strip(), data.get("done_reason") == "length")


class ModelGateway:
    def __init__(self):
        self.providers: dict[str, ModelProvider] = {
            "ollama": OllamaProvider()
        }
        self.specs = {}
        for role, default_model in {
            "source": "llama3.2:latest",
            "destination": "llama3.2:1b",
            "extractor": "llama3.2:latest",
        }.items():
            prefix = role.upper()
            self.specs[role] = {
                "provider": os.getenv(f"{prefix}_PROVIDER", "ollama"),
                "model": os.getenv(f"{prefix}_MODEL", default_model),
                "endpoint": os.getenv(
                    "OLLAMA_URL", "http://127.0.0.1:11434"
                ).rstrip("/"),
                "max_output": int(os.getenv(
                    "EXTRACT_OUTPUT_TOKENS"
                    if role == "extractor" else "CHAT_OUTPUT_TOKENS",
                    "1400" if role == "extractor" else "300",
                )),
            }
            spec = self.specs[role]
            if spec["provider"] not in self.providers:
                raise ValueError(f"Unsupported provider: {spec['provider']}")
            if not 1 <= spec["max_output"] < CONTEXT_WINDOW:
                raise ValueError(f"Invalid output budget for {role}")

    def identity(self, role):
        if not isinstance(role, str) or role not in self.specs:
            raise AppError("Unknown model role.")
        return {
            key: self.specs[role][key]
            for key in ("provider", "model", "endpoint")
        }

    def public_config(self):
        return {role: self.identity(role) for role in self.specs}

    def generate(self, role, messages, schema=None):
        self.identity(role)
        spec = self.specs[role]
        return self.providers[spec["provider"]].generate(
            spec, messages, schema
        )
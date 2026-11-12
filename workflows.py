import hashlib
import json
import secrets
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from runtime import AppError, load_prompt, validate_prompts


CONVERSATION_TTL, TRANSFER_TTL = 1800, 300
MAX_CONVERSATIONS, MAX_TRANSFERS = 100, 100


def encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def message(role, content):
    return {
        "id": secrets.token_hex(4),
        "role": role,
        "content": content,
    }


def render_handoff(package):
    content = package["content"]

    if package["mode"] == "messages":
        return "\n\n".join(
            f'{item["role"].upper()}: {item["content"]}'
            for item in content["messages"]
        )

    return "\n\n".join(
        category.replace("_", " ").upper()
        + "\n"
        + "\n".join(f'- {item["text"]}' for item in items)
        for category, items in content["summary"].items()
        if items
    )


def selected_snapshot(source, selected_user_ids, source_revision, mode):
    if mode not in ("summary", "messages"):
        raise AppError("mode must be summary or messages.")

    if type(source_revision) is not int or source_revision != source["revision"]:
        raise AppError(
            "Source changed. Reopen the selection window.",
            409,
        )

    if (
        not isinstance(selected_user_ids, list)
        or not selected_user_ids
        or len(selected_user_ids) > 120
        or not all(isinstance(value, str) for value in selected_user_ids)
    ):
        raise AppError("Select at least one user message.")

    ids = set(selected_user_ids)

    if len(ids) != len(selected_user_ids):
        raise AppError("Duplicate selected message IDs.")

    history = source["messages"]
    allowed = {
        item["id"] for item in history if item["role"] == "user"
    }

    if not ids.issubset(allowed):
        raise AppError(
            "Selection contains unknown or non-user message IDs."
        )

    included, ordered_ids = [], []

    for index, item in enumerate(history):
        if item["role"] != "user" or item["id"] not in ids:
            continue

        ordered_ids.append(item["id"])
        included.append(item)

        if mode == "messages" and index + 1 < len(history):
            reply = history[index + 1]
            if reply["role"] == "assistant":
                included.append(reply)

    return included, ordered_ids


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=400)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)


class ProjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_goal: list[Fact] = Field(max_length=6)
    requirements: list[Fact] = Field(max_length=6)
    constraints: list[Fact] = Field(max_length=6)
    decisions: list[Fact] = Field(max_length=6)
    rejected_options: list[Fact] = Field(max_length=6)
    current_progress: list[Fact] = Field(max_length=6)
    open_questions: list[Fact] = Field(max_length=6)
    next_task: list[Fact] = Field(max_length=6)


class State(TypedDict, total=False):
    action: str
    request: dict
    source: dict
    destination: dict
    selection: dict
    summary: dict | None
    result: dict


class BridgeService:
    """Memory-only service. app.py serializes access using its shared lock."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.conversations, self.transfers = {}, {}
        validate_prompts()

        graph = StateGraph(State)
        nodes = {
            "chat": self._chat,
            "extract": self._extract,
            "package": self._package,
            "import": self._import,
        }

        for name, function in nodes.items():
            graph.add_node(name, function)

        graph.add_conditional_edges(
            START,
            lambda state: state["action"],
            {
                "chat": "chat",
                "prepare": "extract",
                "commit": "import",
            },
        )
        graph.add_edge("extract", "package")

        for name in ("chat", "package", "import"):
            graph.add_edge(name, END)

        self.graph = graph.compile()

    def _run(self, action, **request):
        return self.graph.invoke({
            "action": action,
            "request": request,
        })["result"]

    def cleanup(self):
        current = time.monotonic()

        expired = {
            key for key, (expiry, _) in self.conversations.items()
            if expiry <= current
        }

        self._remove(expired)

        for key, (expiry, _) in list(self.transfers.items()):
            if expiry <= current:
                del self.transfers[key]

    def _remove(self, ids):
        deleted_chats = sum(
            self.conversations.pop(key, None) is not None
            for key in ids
        )

        related = [
            key for key, (_, transfer) in self.transfers.items()
            if any(
                transfer["package"][side]["conversation_id"] in ids
                for side in ("source", "destination")
            )
        ]

        for key in related:
            del self.transfers[key]

        return {
            "deleted_conversations": deleted_chats,
            "deleted_transfers": len(related),
        }

    def _save(self, store, record, ttl):
        store[record["id"]] = (
            time.monotonic() + ttl,
            deepcopy(record),
        )

    def _read(self, store, record_id, refresh=False):
        self.cleanup()

        if not isinstance(record_id, str):
            raise AppError("A record ID is required.")

        if record_id not in store:
            raise AppError("Record not found or expired.", 404)

        _, record = store[record_id]

        if refresh:
            store[record_id] = (
                time.monotonic() + CONVERSATION_TTL,
                record,
            )

        return deepcopy(record)

    def get_conversation(self, conversation_id):
        return self._read(
            self.conversations,
            conversation_id,
            refresh=True,
        )

    def get_transfer(self, transfer_id):
        return self._read(self.transfers, transfer_id)

    def _check_model(self, conversation):
        if conversation["role"] is None:
            raise AppError("Imported conversation has no generation model.")

        if self.gateway.identity(conversation["role"]) != conversation["model"]:
            raise AppError(
                "Model changed. Start a new conversation.",
                409,
            )

    def create_conversation(self, role=None, messages=None):
        self.cleanup()

        if role is not None and role not in ("source", "destination"):
            raise AppError("role must be source, destination, or null.")

        if len(self.conversations) >= MAX_CONVERSATIONS:
            raise AppError("Active conversation limit reached.", 429)

        messages = [] if messages is None else messages

        if not isinstance(messages, list) or len(messages) > 120:
            raise AppError("messages must be a list of at most 120 entries.")

        try:
            parsed = [Message.model_validate(item) for item in messages]
        except ValueError as exc:
            raise AppError("Invalid imported messages.") from exc

        record = {
            "id": secrets.token_urlsafe(18),
            "role": role,
            "model": self.gateway.identity(role) if role else None,
            "messages": [
                message(item.role, item.content)
                for item in parsed
            ],
            "handoff": None,
            "revision": 1 if parsed else 0,
            "created_at": now(),
        }

        self._save(self.conversations, record, CONVERSATION_TTL)
        return record

    def chat(self, conversation_id, text):
        return self._run(
            "chat",
            conversation_id=conversation_id,
            text=text,
        )

    def continue_chat(self, conversation_id):
        if not self.get_conversation(conversation_id)["handoff"]:
            raise AppError("Import a handoff first.")

        return self.chat(
            conversation_id,
            load_prompt("continue_project.txt"),
        )

    def _chat(self, state):
        request = state["request"]
        text = request["text"]

        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise AppError("message must contain 1–12,000 characters.")

        conversation = self.get_conversation(request["conversation_id"])
        self._check_model(conversation)

        if len(conversation["messages"]) > 118:
            raise AppError("Message limit reached. Start a new conversation.")

        system = load_prompt("chat_system.txt")

        if conversation["handoff"]:
            system += "\n" + load_prompt(
                "handoff_reference.txt",
                handoff=render_handoff(conversation["handoff"]),
            )

        messages = [{"role": "system", "content": system}]
        messages += [
            {"role": item["role"], "content": item["content"]}
            for item in conversation["messages"]
        ]
        messages.append({"role": "user", "content": text.strip()})

        result = self.gateway.generate(conversation["role"], messages)

        current = self.get_conversation(conversation["id"])

        if current["revision"] != conversation["revision"]:
            raise AppError("Conversation changed. Retry.", 409)

        current["messages"] += [
            message("user", text.strip()),
            message("assistant", result.text),
        ]
        current["revision"] += 1

        self._save(self.conversations, current, CONVERSATION_TTL)

        return {"result": {
            "conversation": current,
            "truncated": result.truncated,
        }}

    def prepare(
        self,
        source_id,
        destination_id,
        mode,
        selected_user_message_ids=None,
        source_revision=None,
    ):
        return self._run(
            "prepare",
            source_id=source_id,
            destination_id=destination_id,
            mode=mode,
            selected_user_message_ids=selected_user_message_ids,
            source_revision=source_revision,
        )

    def _extract(self, state):
        self.cleanup()
        request = state["request"]

        if request["source_id"] == request["destination_id"]:
            raise AppError("Source and destination must differ.")

        if len(self.transfers) >= MAX_TRANSFERS:
            raise AppError("Prepared transfer limit reached.", 429)

        source = self.get_conversation(request["source_id"])
        destination = self.get_conversation(request["destination_id"])
        self._check_model(destination)

        source_count = len(source["messages"])

        included, ordered_ids = selected_snapshot(
            source,
            request["selected_user_message_ids"],
            request["source_revision"],
            request["mode"],
        )

        # This is a detached snapshot, not the stored source conversation.
        source["messages"] = included

        selection = {
            "policy": (
                "Selected user messages and immediate assistant replies"
                if request["mode"] == "messages"
                else "Selected user messages only"
            ),
            "selected_user_message_ids": ordered_ids,
            "included_message_ids": [item["id"] for item in included],
            "selected_user_message_count": len(ordered_ids),
            "included_message_count": len(included),
            "source_message_count": source_count,
        }

        context = {
            "source": source,
            "destination": destination,
            "selection": selection,
        }

        if request["mode"] == "messages":
            return {**context, "summary": None}

        schema = ProjectContext.model_json_schema()
        valid_ids = {item["id"] for item in included}
        retry = ""

        for _ in range(2):
            result = self.gateway.generate(
                "extractor",
                [
                    {
                        "role": "system",
                        "content": load_prompt("extract_system.txt"),
                    },
                    {
                        "role": "user",
                        "content": load_prompt(
                            "extract_request.txt",
                            schema=encode(schema),
                            transcript=encode(included),
                            retry_note=retry,
                        ),
                    },
                ],
                schema=schema,
            )

            try:
                if result.truncated:
                    raise ValueError(
                        "Extraction reached its output-token limit."
                    )

                summary = ProjectContext.model_validate_json(
                    result.text
                ).model_dump()

                if not any(summary.values()):
                    raise ValueError("No usable project facts extracted.")

                if any(
                    not set(item["evidence_ids"]).issubset(valid_ids)
                    for items in summary.values()
                    for item in items
                ):
                    raise ValueError(
                        "Evidence references an excluded or unknown message."
                    )

                return {**context, "summary": summary}

            except ValueError as exc:
                retry = load_prompt(
                    "extract_retry.txt",
                    error=str(exc)[:250],
                )

        raise AppError(
            "Summary validation failed twice. Try raw-message mode.",
            422,
        )

    def _package(self, state):
        source = state["source"]
        destination = state["destination"]
        mode = state["request"]["mode"]

        current_source = self.get_conversation(source["id"])
        current_destination = self.get_conversation(destination["id"])

        if (
            current_source["revision"] != source["revision"]
            or current_destination["revision"] != destination["revision"]
        ):
            raise AppError(
                "Conversation changed during preparation. Select again.",
                409,
            )

        package = {
            "schema_version": "1.1",
            "created_at": now(),
            "mode": mode,
            "scope": "Explicitly selected source user turns",
            "source": {
                "conversation_id": source["id"],
                "revision": source["revision"],
                "model": source["model"],
                "origin_verified": False,
            },
            "destination": {
                "conversation_id": destination["id"],
                "model": destination["model"],
            },
            "selection": state["selection"],
            "extractor": (
                self.gateway.identity("extractor")
                if mode == "summary"
                else None
            ),
            "content": (
                {"messages": source["messages"]}
                if mode == "messages"
                else {"summary": state["summary"]}
            ),
        }

        transfer = {
            "id": secrets.token_urlsafe(18),
            "package": package,
            "payload_sha256": digest(package),
            "destination_revision": destination["revision"],
            "status": "prepared",
            "approved_at": None,
        }

        self._save(self.transfers, transfer, TRANSFER_TTL)
        return {"result": transfer}

    def commit(self, transfer_id, approved, approved_hash):
        return self._run(
            "commit",
            transfer_id=transfer_id,
            approved=approved,
            approved_hash=approved_hash,
        )

    def _import(self, state):
        request = state["request"]
        transfer = self.get_transfer(request["transfer_id"])

        if request["approved"] is not True:
            raise AppError("Explicit approved=true is required.", 403)

        approved_hash = request["approved_hash"]

        if not isinstance(approved_hash, str) or not secrets.compare_digest(
            approved_hash.encode(),
            transfer["payload_sha256"].encode(),
        ):
            raise AppError("Approval does not match the payload.", 409)

        if digest(transfer["package"]) != approved_hash:
            raise AppError("Handoff integrity check failed.", 409)

        if transfer["status"] == "imported":
            return {"result": {
                **transfer,
                "already_imported": True,
            }}

        target = transfer["package"]["destination"]
        destination = self.get_conversation(target["conversation_id"])
        self._check_model(destination)

        if (
            destination["revision"] != transfer["destination_revision"]
            or destination["model"] != target["model"]
        ):
            raise AppError(
                "Destination changed. Prepare a fresh handoff.",
                409,
            )

        destination["handoff"] = transfer["package"]
        destination["revision"] += 1
        self._save(self.conversations, destination, CONVERSATION_TTL)

        transfer.update(status="imported", approved_at=now())
        expiry, _ = self.transfers[transfer["id"]]
        self.transfers[transfer["id"]] = (
            expiry,
            deepcopy(transfer),
        )

        return {"result": {
            **transfer,
            "already_imported": False,
        }}

    def clear_conversations(self, conversation_ids):
        if (
            not isinstance(conversation_ids, list)
            or len(conversation_ids) > MAX_CONVERSATIONS
            or not all(isinstance(item, str) for item in conversation_ids)
        ):
            raise AppError("conversation_ids must be a list of IDs.")

        return self._remove(set(conversation_ids))
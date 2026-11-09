import os
import secrets
import threading
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

from runtime import PORT, AppError, ModelGateway
from workflows import BridgeService


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

API_TOKEN = os.getenv("BRIDGE_API_TOKEN", "").strip()

if (
    len(API_TOKEN) < 24
    or API_TOKEN == "replace-this-with-a-generated-random-token"
):
    raise RuntimeError(
        "Set BRIDGE_API_TOKEN in .env to a generated random value."
    )

gateway = ModelGateway()
bridge = BridgeService(gateway)

operation_lock = threading.RLock()


def cleanup_loop():
    while True:
        threading.Event().wait(30)
        with operation_lock:
            bridge.cleanup()


def json_body():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        raise AppError("Expected a JSON object.")

    return data


@app.before_request
def protect_local_application():
    if request.host not in {
        f"127.0.0.1:{PORT}",
        f"localhost:{PORT}",
    }:
        raise AppError(
            "Invalid host. This prototype is loopback-only.",
            400,
        )

    origin = request.headers.get("Origin")

    if origin and urlparse(origin).netloc != request.host:
        raise AppError(
            "Cross-origin browser requests are not allowed.",
            403,
        )

    if request.path.startswith("/api/v1/"):
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {API_TOKEN}"

        if not secrets.compare_digest(
            supplied.encode("utf-8"),
            expected.encode("utf-8"),
        ):
            raise AppError(
                "A valid Bearer token is required.",
                401,
            )


@app.after_request
def add_security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    return response


@app.errorhandler(AppError)
def handle_expected_error(error):
    return jsonify(error=str(error)), error.status


@app.errorhandler(413)
def handle_large_request(error):
    return jsonify(error="Request exceeds 1 MiB."), 413


@app.errorhandler(500)
def handle_unexpected_error(error):
    return jsonify(
        error="Unexpected server error. Check the Flask terminal."
    ), 500


@app.get("/")
def index():
    return render_template(
        "index.html",
        api_token=API_TOKEN,
    )


@app.get("/api/v1/health")
def health():
    return jsonify(
        status="ok",
        api_version="1.0",
        persistence="memory-only",
        conversation_idle_timeout_seconds=30 * 60,
        transfer_timeout_seconds=5 * 60,
    )


@app.get("/api/v1/config")
def configuration():
    return jsonify(models=gateway.public_config())


@app.post("/api/v1/conversations")
def create_conversation():
    data = json_body()

    with operation_lock:
        result = bridge.create_conversation(
            role=data.get("role"),
            messages=data.get("messages"),
        )

    return jsonify(result), 201


@app.get("/api/v1/conversations/<conversation_id>")
def get_conversation(conversation_id):
    with operation_lock:
        return jsonify(
            bridge.get_conversation(conversation_id)
        )


@app.post("/api/v1/conversations/<conversation_id>/messages")
def send_message(conversation_id):
    data = json_body()

    with operation_lock:
        result = bridge.chat(
            conversation_id,
            data.get("message"),
        )

    return jsonify(result)


@app.post("/api/v1/conversations/<conversation_id>/continue")
def continue_conversation(conversation_id):
    with operation_lock:
        result = bridge.continue_chat(conversation_id)

    return jsonify(result)


@app.post("/api/v1/transfers/prepare")
def prepare_transfer():
    data = json_body()

    with operation_lock:
        result = bridge.prepare(
            source_id=data.get("source_id"),
            destination_id=data.get("destination_id"),
            mode=data.get("mode", "summary"),
        )

    return jsonify(result), 201


@app.get("/api/v1/transfers/<transfer_id>")
def get_transfer(transfer_id):
    with operation_lock:
        return jsonify(
            bridge.get_transfer(transfer_id)
        )


@app.post("/api/v1/transfers/<transfer_id>/commit")
def commit_transfer(transfer_id):
    data = json_body()

    with operation_lock:
        result = bridge.commit(
            transfer_id=transfer_id,
            approved=data.get("approved"),
            approved_hash=data.get("payload_sha256"),
        )

    return jsonify(result)


@app.post("/api/v1/sessions/clear")
def clear_workspace():
    data = json_body()

    with operation_lock:
        result = bridge.clear_conversations(
            data.get("conversation_ids")
        )

    return jsonify(result)


if __name__ == "__main__":
    threading.Thread(
        target=cleanup_loop,
        daemon=True,
        name="memory-cleanup",
    ).start()

    app.run(
        host="127.0.0.1",
        port=PORT,
        debug=False,
        threaded=True,
    )
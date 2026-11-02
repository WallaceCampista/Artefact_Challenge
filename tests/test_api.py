import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from emporio.agent import ModelUnavailable, Reply  # noqa: E402
from emporio.config import load_settings  # noqa: E402
from emporio.memory import Analytics, ConversationStore, Database, FeedbackStore, SqliteHandoffDesk  # noqa: E402
from emporio.web.api import create_app  # noqa: E402


class FakeService:
    """ChatService stand-in: real SQLite persistence, scripted agent."""

    def __init__(self, tmp_path):
        self.settings = load_settings()
        self.db = Database(tmp_path / "api.db")
        self.conversations = ConversationStore(self.db)
        self.feedback = FeedbackStore(self.db)
        self.analytics = Analytics(self.db)
        self.desk = SqliteHandoffDesk(self.db)
        self.policy_mode = "BM25"
        self.fail = False
        self.clocks = []

    def send(self, session_id, text, simulated_now=None, on_event=None):
        if self.fail:
            raise ModelUnavailable("down")
        self.clocks.append(simulated_now)
        reply = Reply(text=f"Eco: {text}", listener=on_event)
        reply.emit("model_call", n=1, tools_enabled=True)
        reply.emit("guard", check="grounding", outcome="pass", details=[])
        reply.message_id = self.conversations.save_exchange(session_id, text, reply.text, [],
                                                            {"trace": reply.trace, "latency_ms": 5})
        return reply


@pytest.fixture
def service(tmp_path):
    return FakeService(tmp_path)


@pytest.fixture
def client(service):
    return TestClient(create_app(service=service))


def test_chat_persists_and_returns_trace(client, service):
    body = client.post("/api/chat", json={"session_id": "web-1", "message": "oi",
                                          "simulated_now": "2026-11-01T10:00:00"}).json()
    assert body["text"] == "Eco: oi" and [e["type"] for e in body["trace"]] == ["model_call", "guard"]
    assert service.clocks == ["2026-11-01T10:00:00"]
    messages = client.get("/api/sessions/web-1").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"] and messages[1]["trace"]


def test_stream_sends_trace_events_then_the_answer(client):
    with client.stream("POST", "/api/chat/stream", json={"session_id": "web-2", "message": "oi"}) as response:
        raw = "".join(response.iter_text())
    events = [(block.split("\n")[0][7:], json.loads(block.split("\n")[1][6:]))
              for block in raw.strip().split("\n\n")]
    assert [kind for kind, _ in events] == ["trace", "trace", "answer"]
    assert events[-1][1]["text"] == "Eco: oi" and events[-1][1]["message_id"]


def test_model_down_gives_503_and_stream_error(client, service):
    service.fail = True
    assert client.post("/api/chat", json={"session_id": "s", "message": "oi"}).status_code == 503
    with client.stream("POST", "/api/chat/stream", json={"session_id": "s", "message": "oi"}) as response:
        assert "event: error" in "".join(response.iter_text())


def test_input_validation(client):
    assert client.post("/api/chat", json={"session_id": "bad id!", "message": "oi"}).status_code == 422
    assert client.post("/api/chat", json={"session_id": "s", "message": ""}).status_code == 422
    assert client.post("/api/chat", json={"session_id": "s", "message": "oi", "simulated_now": "ontem"}).status_code == 422


def test_feedback_handoffs_and_metrics(client, service):
    answer = client.post("/api/chat", json={"session_id": "s3", "message": "oi"}).json()
    assert client.post("/api/feedback", json={"message_id": answer["message_id"], "rating": "up"}).json() == {"ok": True}
    assert client.post("/api/feedback", json={"message_id": 999, "rating": "up"}).status_code == 404
    protocol = service.desk.open("reclamacao", "teste", None, "s3", __import__("datetime").date(2026, 11, 3))
    assert client.get("/api/handoffs?status=open").json()["handoffs"][0]["protocol"] == protocol
    assert client.post(f"/api/handoffs/{protocol}/resolve").json() == {"ok": True}
    assert client.post(f"/api/handoffs/{protocol}/resolve").status_code == 404
    metrics = client.get("/api/metrics").json()
    assert metrics["answers"] == 1 and metrics["feedback"]["up"] == 1 and metrics["handoffs"]["resolved"] == 1


def test_index_and_config_are_served(client):
    page = client.get("/").text
    assert "/static/app.js" in page and "/static/styles.css" in page
    for asset in ("app.js", "styles.css"):
        assert client.get(f"/static/{asset}").status_code == 200
    config = client.get("/api/config").json()
    assert config["model"] and any(c["value"] for c in config["clocks"])

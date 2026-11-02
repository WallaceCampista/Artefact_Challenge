from datetime import date

from emporio.memory import ConversationStore, Database, SqliteHandoffDesk
from emporio.tools import ToolCall


def test_history_keeps_last_exchanges_in_order(tmp_path):
    db = Database(tmp_path / "test.db")
    conversations = ConversationStore(db)
    for i in range(5):
        conversations.save_exchange("67999990000", f"pergunta {i}", f"resposta {i}", [])
    history = conversations.history("67999990000", turns=2)
    assert [m["content"] for m in history] == ["pergunta 3", "resposta 3", "pergunta 4", "resposta 4"]
    assert conversations.history("other-session", turns=2) == []


def test_history_survives_reopening_the_database(tmp_path):
    path = tmp_path / "test.db"
    calls = [ToolCall("consultar_pedido", {"numero_pedido": 8}, {"status": "enviado"}, 1.0)]
    ConversationStore(Database(path)).save_exchange("s1", "meu pedido 8?", "Está enviado.", calls)
    reopened = ConversationStore(Database(path))
    assert reopened.count("s1") == 2
    assert reopened.history("s1", turns=6)[-1]["content"] == "Está enviado."


def test_handoff_protocols_are_unique_and_persisted(tmp_path):
    desk = SqliteHandoffDesk(Database(tmp_path / "test.db"))
    first = desk.open("reclamacao", "Produto com risco", "Ana", "s1", date(2026, 11, 3))
    second = desk.open("atendimento_humano", "Quer falar com vendedor", None, "s2", date(2026, 11, 3))
    assert first == "EM-20261103-0001" and second == "EM-20261103-0002"
    assert [t["protocol"] for t in desk.list_open()] == [first, second]


def test_tool_trail_returns_recent_tool_calls(tmp_path):
    from emporio.memory import ConversationStore, Database
    from emporio.tools import ToolCall

    conversations = ConversationStore(Database(tmp_path / "t.db"))
    conversations.save_exchange("s", "oi", "olá", [])
    conversations.save_exchange("s", "pedido 7", "achei", [ToolCall("consultar_pedido", {"numero_pedido": 7}, {"encontrado": True}, 1.0)])
    trail = conversations.tool_trail("s", 6)
    assert [c["name"] for c in trail] == ["consultar_pedido"] and trail[0]["result"] == {"encontrado": True}


def test_metrics_feedback_and_ticket_resolution(tmp_path):
    from emporio.memory import Analytics, ConversationStore, Database, FeedbackStore, SqliteHandoffDesk
    from emporio.tools import ToolCall

    db = Database(tmp_path / "m.db")
    conversations, feedback = ConversationStore(db), FeedbackStore(db)
    trace = [{"type": "tool", "name": "consultar_horario", "auto": True},
             {"type": "guard", "check": "grounding", "outcome": "retry"}]
    first = conversations.save_exchange("s1", "abre amanhã?", "Fechado.", [ToolCall("consultar_horario", {}, {}, 1.0)],
                                        {"trace": trace, "latency_ms": 4000, "grounding_retries": 1, "redactions": 0})
    second = conversations.save_exchange("s2", "oi", "Olá!", [], {"latency_ms": 2000})
    assert feedback.rate(first, "down", "não abre?") and feedback.rate(second, "up")
    assert not feedback.rate(999, "up")
    assert feedback.negatives()[0]["question"] == "abre amanhã?"

    desk = SqliteHandoffDesk(db)
    protocol = desk.open("reclamacao", "x", None, "s1", date(2026, 11, 3))
    summary = Analytics(db).summary()
    assert summary["conversations"] == 2 and summary["answers"] == 2
    assert summary["latency_ms"]["avg"] == 3000 and summary["tool_usage"] == {"consultar_horario": 1}
    assert summary["pre_routed_tool_calls"] == 1 and summary["guards"] == {"grounding:retry": 1}
    assert summary["handoff_rate"] == 0.5 and summary["feedback"] == {"up": 1, "down": 1}

    assert desk.resolve(protocol) and not desk.resolve(protocol)
    assert desk.list("resolved")[0]["protocol"] == protocol and desk.list_open() == []
    transcript = conversations.transcript("s1")
    assert transcript[1]["trace"] == trace and transcript[1]["rating"] == "down"


def test_old_databases_are_migrated_in_place(tmp_path):
    import sqlite3

    from emporio.memory import ConversationStore, Database

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                      "role TEXT NOT NULL, content TEXT NOT NULL, tool_calls TEXT, created_at TEXT NOT NULL);")
    old.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES ('s', 'user', 'oi', 'x')")
    old.commit()
    old.close()
    conversations = ConversationStore(Database(path))
    conversations.save_exchange("s", "tudo bem?", "Tudo!", [], {"latency_ms": 10})
    assert conversations.count("s") == 3

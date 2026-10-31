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

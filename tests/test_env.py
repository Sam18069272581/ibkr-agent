import os

from ibkr_agent.env import load_dotenv


def test_load_dotenv(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# c\nA_X=1\nB_X="Name a@b.com"\nC_X=\nD_X=keep\n', encoding="utf-8")
    for k in ("A_X", "B_X", "C_X"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("D_X", "orig")
    load_dotenv(f)
    assert os.environ["A_X"] == "1"
    assert os.environ["B_X"] == "Name a@b.com"
    assert "C_X" not in os.environ
    assert os.environ["D_X"] == "orig"

import os

from safechange import envfile


def test_parse_handles_comments_export_and_quotes():
    text = '# c\n\nexport A=1\nB = "two words"\nC=\'x=y\'\nbad line\n=nokey\n'
    assert envfile.parse(text) == {"A": "1", "B": "two words", "C": "x=y"}


def test_load_does_not_override_existing(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("SC_TEST_NEW=file\nSC_TEST_OLD=file\n")
    monkeypatch.setenv("SC_TEST_OLD", "shell")
    monkeypatch.delenv("SC_TEST_NEW", raising=False)
    assert envfile.load(f) == ["SC_TEST_NEW"]
    assert os.environ["SC_TEST_NEW"] == "file"
    assert os.environ["SC_TEST_OLD"] == "shell"
    monkeypatch.delenv("SC_TEST_NEW")


def test_load_missing_file_is_noop(tmp_path):
    assert envfile.load(tmp_path / "nope") == []

"""Smoke test: the Streamlit page loads and 'New subject' works. No API calls."""
from streamlit.testing.v1 import AppTest

import config


def test_app_loads_and_starts_subject(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")  # never touch the real DB
    at = AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30).run()
    assert not at.exception
    assert any("New subject" in i.value for i in at.info)

    at.button[0].click().run()  # "+ New subject"
    assert not at.exception
    assert at.subheader[0].value == "Let's scope your subject"
    assert any("What would you like to learn" in m.markdown[0].value for m in at.chat_message)

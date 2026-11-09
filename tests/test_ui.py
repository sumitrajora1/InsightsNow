"""Optional UI integration: run against an already running labelled synthetic API."""

import os
import time

import pytest

from app.config import ROOT

pytestmark = pytest.mark.skipif(
    os.environ.get("EDQ_RUN_UI_TESTS") != "1", reason="Requires local running API"
)


def test_streamlit_question_result_and_new_conversation():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(ROOT / "ui/app.py", default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Lending Data Desk"
    next(b for b in at.button if "New conversation" in b.label).click().run()
    # Identical result shapes/figures in one history must have separate widget IDs.
    for count in range(1, 3):
        example = next(b for b in at.button if b.label.startswith("Show corporate lending exposure"))
        example.click().run()
        for _ in range(100):
            time.sleep(0.1)
            at.run()
            assert not at.exception
            if len(at.get("plotly_chart")) == count:
                break
        assert len(at.get("plotly_chart")) == count
    assert at.get("dataframe")
    assert any("SYNTHETIC" in x.value for x in at.markdown)
    new = next(b for b in at.button if "New conversation" in b.label)
    new.click().run()
    assert not at.exception
    assert not at.chat_message

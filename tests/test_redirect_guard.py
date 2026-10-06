"""SSRF regression: removed network tool is unavailable by default."""
from tests.conftest import tmp_db

def test_http_get_not_registered_in_engineering_runtime(workdir):
    s = tmp_db(workdir)
    try:
        assert "http_get" not in s.tool_names()
        assert "make_purchase" not in s.tool_names()
        assert "send_notification" not in s.tool_names()
    finally:
        s.close()

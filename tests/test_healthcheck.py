import os

from app import healthcheck


def test_healthcheck(tmp_path):
    path = tmp_path / "status.json"
    assert healthcheck.main(str(path)) == 1                         # missing
    path.write_text("{}")
    assert healthcheck.main(str(path)) == 0
    assert healthcheck.main(str(path), now=os.path.getmtime(path) + healthcheck.MAX_AGE + 1) == 1

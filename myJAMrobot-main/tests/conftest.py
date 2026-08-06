from __future__ import annotations

import os

os.environ.setdefault("MYJAM_DATA_DIR", "/tmp/myjamrobot-tests")
os.environ.setdefault("MYJAM_DATABASE_URL", "sqlite:////tmp/myjamrobot-tests/test.sqlite3")

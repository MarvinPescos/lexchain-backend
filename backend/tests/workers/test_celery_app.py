import subprocess
import sys


def test_celery_app_imports_first():
    """Workers load `app.celery_app` before anything else, so a circular import
    there stops every worker while the API and the rest of the tests still pass.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import app.celery_app"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

import pytest

from app.celery_tasks import runner


def test_pool_is_disposed_after_each_task_even_on_failure(monkeypatch):
    disposed = []

    class Engine:
        async def dispose(self):
            disposed.append(True)

    monkeypatch.setattr(runner, "async_engine", Engine())

    async def ok():
        return 42

    async def boom():
        raise RuntimeError("job failed")

    assert runner.run_async(ok()) == 42
    with pytest.raises(RuntimeError):
        runner.run_async(boom())
    assert disposed == [True, True]

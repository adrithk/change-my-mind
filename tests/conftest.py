import pytest

from change_my_mind.schemas import RunCreate
from change_my_mind.store import Store


@pytest.fixture
async def store(tmp_path):
    instance = Store(tmp_path)
    await instance.initialize()
    yield instance
    await instance.close()


@pytest.fixture
async def run_id(store):
    return await store.create_run(
        RunCreate(question="Should the library extend evening hours?", model_config={"provider": "demo"})
    )

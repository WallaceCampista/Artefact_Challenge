import pytest

from emporio.catalog import Store
from emporio.config import PROJECT_ROOT


@pytest.fixture(scope="session")
def store() -> Store:
    return Store.load(PROJECT_ROOT / "data")

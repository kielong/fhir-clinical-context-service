# ORIGIN: AI — shared test fixtures typed by Claude Code, reviewed by Kiel.
import pytest
import respx


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def hapi():
    """A fake HAPI server. Any request a test did not register fails loudly, so a test can never
    pass by accident because the code quietly called something unexpected.

    Routes match in the order they are registered, and a route without query params matches any
    query string: register the most specific route first.
    """
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def ollama(hapi):
    """The same fake network as `hapi`: one router fakes every server, so a test registers its
    Ollama routes on it too. (Named separately so a test reads as being about the model.)"""
    return hapi

"""Socket permissions for tests that exercise the actual local media gateway."""

import pytest


@pytest.fixture
def loopback_sockets(socket_enabled):
    """Enable sockets after setup hooks, while keeping remote connections blocked."""
    import pytest_socket

    pytest_socket.socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)

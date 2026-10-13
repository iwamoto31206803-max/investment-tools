"""Tests are offline, including legacy Phase 1 tests."""
import socket

import pytest


@pytest.fixture(autouse=True)
def prohibit_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live network access is forbidden in automated tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

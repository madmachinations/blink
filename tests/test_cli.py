import pytest
from blink_live_client import cli
from blink_live_client.errors import BlinkError


def test_credential_lock_is_private_exclusive_and_reusable(tmp_path):
    state = tmp_path / "private" / "auth.json"
    with cli.credential_lock(state):
        assert state.parent.stat().st_mode & 0o777 == 0o700
        assert state.with_suffix(".lock").stat().st_mode & 0o777 == 0o600
        with pytest.raises(BlinkError, match="Another Blink Live process"):
            with cli.credential_lock(state):
                pytest.fail("Second owner acquired the lock")
    with cli.credential_lock(state):
        pass


def test_credential_lock_released_after_exception(tmp_path):
    state = tmp_path / "auth.json"
    with pytest.raises(RuntimeError):
        with cli.credential_lock(state):
            raise RuntimeError("command failed")
    with cli.credential_lock(state):
        pass


def test_credential_lock_refuses_symlink(tmp_path):
    state = tmp_path / "auth.json"
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    state.with_suffix(".lock").symlink_to(target)
    with pytest.raises(OSError):
        with cli.credential_lock(state):
            pytest.fail("Symlink lock must not be opened")
    assert target.read_text() == "unchanged"


def test_login_entrypoint_acquires_lock_before_running_command(tmp_path, monkeypatch):
    state = tmp_path / "private" / "auth.json"
    started = []

    async def run(args):
        assert args.command == "login"
        assert args.state == state
        with pytest.raises(BlinkError):
            with cli.credential_lock(state):
                pytest.fail("Command ran without holding its credential lock")
        started.append(True)

    monkeypatch.setattr("sys.argv", ["blink-live", "--state", str(state), "login"])
    monkeypatch.setattr(cli, "run", run)
    cli.main()
    assert started == [True]

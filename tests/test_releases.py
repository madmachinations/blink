import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest
from packaging.version import Version


def test_actual_integration_bundles_push_implementation_and_notices(release_tools, tmp_path):
    build, _ = release_tools
    root = Path(__file__).resolve().parents[1]
    archive_path = build.package_integration(root, tmp_path / "integration.zip")
    with ZipFile(archive_path) as archive:
        prefix = "custom_components/blink_live/blink_live_client/"
        for name in (
            "push.py",
            "push_proto.py",
            "push_registration.py",
            "notification_log.py",
            "NOTICE",
        ):
            assert prefix + name in archive.namelist()
        assert prefix + "event_log.py" not in archive.namelist()
        notice = archive.read(prefix + "NOTICE").decode()
        assert "Corey Hayward" in notice and "2024 Your Name" in notice
        assert all("ha-blink-live-events/" not in name for name in archive.namelist())
        for name in ("push.py", "push_proto.py", "push_registration.py"):
            source = archive.read(prefix + name).decode()
            assert "from receiver" not in source and "import blinkpy" not in source


@pytest.fixture
def release_tools(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("build_artifacts"), importlib.import_module("publish_release")


@pytest.fixture
def release_source(tmp_path):
    root = tmp_path / "source"
    component = root / "custom_components" / "blink_live"
    component.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "blink-live-cli"\nversion = "0.1.0"\n')
    for name in ("LICENSE", "README.md"):
        (root / name).write_text(name)
    (component / "manifest.json").write_text('{"domain": "blink_live", "version": "0.1.0"}')
    (component / "__init__.py").write_text("# integration\n")
    (component / "__pycache__").mkdir()
    (component / "__pycache__" / "private.pyc").write_bytes(b"ignored")
    (root / ".blink-live").mkdir()
    (root / ".blink-live" / "auth.json").write_text("SECRET")
    (root / "reference").mkdir()
    (root / "reference" / "original.js").write_text("not distributable")
    return root


def test_date_version_uses_utc_and_padded_hours(release_tools):
    build, _ = release_tools
    assert build.release_version(datetime(2026, 10, 19, 16, 26, tzinfo=UTC)) == "2610.2921626"
    assert build.release_version(datetime(2027, 1, 1, 0, 5, tzinfo=UTC)) == "2701.10005"
    assert build.release_version(datetime(2028, 2, 29, 9, 7, tzinfo=UTC)) == "2802.600907"
    assert build.release_version(
        datetime(2026, 10, 19, 17, 26, tzinfo=ZoneInfo("Europe/London"))
    ) == ("2610.2921626")


def test_versions_increase_overnight_and_across_daylight_saving_change(release_tools):
    build, _ = release_tools
    times = [
        datetime(2026, 10, 19, 23, 59, tzinfo=UTC),
        datetime(2026, 10, 20, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 59, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
    ]
    versions = [Version(build.release_version(instant)) for instant in times]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)


def test_naive_build_timestamp_is_rejected(release_tools):
    build, _ = release_tools
    with pytest.raises(ValueError, match="timezone"):
        build.release_version(datetime(2026, 10, 19, 16, 26))


@pytest.mark.parametrize("version", ["0.1.0", "../tag", "2610.0", "2610.29216;bad"])
def test_bad_release_versions_are_rejected(release_tools, version):
    build, _ = release_tools
    with pytest.raises(ValueError):
        build.validate_version(version)


def test_artifact_build_stamps_only_staging_and_removes_old_distributions(
    release_tools,
    release_source,
    tmp_path,
    monkeypatch,
):
    build, _ = release_tools
    output = tmp_path / "dist"
    output.mkdir()
    for name in ("blink_live.zip", "blink_live-0.1.0.tar.gz", "blink_live-0.1.0-py3-none-any.whl"):
        (output / name).write_text("old artifact")
    (output / "unrelated.txt").write_text("preserve")

    def run(command, **kwargs):
        assert "--wheel" in command and "--sdist" not in command
        stage = Path(command[-1])
        assert 'version = "2610.2921626"' in (stage / "pyproject.toml").read_text()
        assert not (stage / ".blink-live").exists()
        assert not (stage / "reference").exists()
        wheel_dir = Path(command[command.index("--outdir") + 1])
        wheel_dir.mkdir()
        with ZipFile(wheel_dir / "blink_live_cli-2610.2921626-py3-none-any.whl", "w") as wheel:
            wheel.writestr("blink_live_client/__init__.py", "# wheel")

    monkeypatch.setattr(build.subprocess, "run", run)
    artifacts = build.build_artifacts(release_source, output, "2610.2921626")
    assert {path.name for path in artifacts} == {
        "blink-live-homeassistant.zip",
        "blink_live_cli-2610.2921626-py3-none-any.whl",
    }
    with ZipFile(artifacts[0]) as archive:
        assert json.loads(archive.read("custom_components/blink_live/manifest.json"))[
            "version"
        ] == ("2610.2921626")
        assert all(
            "__pycache__" not in name and "auth.json" not in name for name in archive.namelist()
        )
    assert len(list(output.iterdir())) == 3
    assert (output / "unrelated.txt").read_text() == "preserve"
    assert 'version = "0.1.0"' in (release_source / "pyproject.toml").read_text()


@pytest.fixture
def release_assets(tmp_path):
    for name in ("blink-live-homeassistant.zip", "blink_live_cli-2610.2921626-py3-none-any.whl"):
        (tmp_path / name).write_bytes(b"artifact")
    return tmp_path


def test_publish_uploads_draft_before_exposing_release(release_tools, release_assets, monkeypatch):
    _, publish = release_tools
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="[]")

    monkeypatch.setattr(publish.subprocess, "run", run)
    publish.publish_release("2610.2921626", release_assets, "owner/repo", "a" * 40)
    assert len(commands) == 3
    create, expose = commands[1:]
    assert create[1:4] == ["release", "create", "2610.2921626"]
    assert "--draft" in create
    assert create[create.index("--target") + 1] == "a" * 40
    assert "--draft=false" in expose
    assert any("blink-live-homeassistant.zip#" in argument for argument in create)
    assert not any(".tar.gz" in argument for argument in create)


def test_existing_tag_is_never_overwritten(release_tools, release_assets, monkeypatch):
    _, publish = release_tools
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='[{"ref":"refs/tags/2610.2921626"}]')

    monkeypatch.setattr(publish.subprocess, "run", run)
    with pytest.raises(ValueError, match="already exists"):
        publish.publish_release("2610.2921626", release_assets, "owner/repo", "a" * 40)
    assert len(calls) == 1


def test_upload_failure_does_not_publish_release(release_tools, release_assets, monkeypatch):
    _, publish = release_tools
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "api":
            return SimpleNamespace(stdout="[]")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(publish.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        publish.publish_release("2610.2921626", release_assets, "owner/repo", "a" * 40)
    assert len(calls) == 2


def test_missing_artifacts_fail_before_github_is_contacted(release_tools, tmp_path, monkeypatch):
    _, publish = release_tools

    def forbidden(*args, **kwargs):
        pytest.fail("Must validate local artifacts before contacting GitHub")

    monkeypatch.setattr(publish.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="Expected one"):
        publish.publish_release("2610.2921626", tmp_path, "owner/repo", "a" * 40)

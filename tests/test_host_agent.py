from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("host_agent", SCRIPTS / "host_agent.py")
assert SPEC and SPEC.loader
host_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host_agent
SPEC.loader.exec_module(host_agent)


def test_stage_artifacts_uses_agent_visible_absolute_paths(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    (build / "k8labs-base.qcow2").write_bytes(b"base-image")
    (build / "CLOUDHV.fd").write_bytes(b"firmware")

    paths = host_agent.stage_artifacts(tmp_path / "agent")

    manifest = json.loads(Path(paths["manifest"]).read_text(encoding="utf-8"))
    assert manifest["images"] == {
        host_agent.BASE_IMAGE_REFERENCE: {
            "path": str(Path(paths["images"]) / "k8labs-base.qcow2"),
            "sha256": host_agent.sha256(Path(paths["images"]) / "k8labs-base.qcow2"),
        }
    }
    assert manifest["firmware"]["path"] == str(Path(paths["firmware"]) / "CLOUDHV.fd")


def test_quadlet_has_one_exec_and_only_bounded_agent_paths(
    tmp_path: Path, monkeypatch
) -> None:
    xdg_config = tmp_path / "config"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bus").touch()
    (runtime / "k8snet").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        host_agent.shutil, "which", lambda _: "/opt/bin/cloud-hypervisor"
    )
    paths = {
        "state": str(tmp_path / "state"),
        "vms": str(tmp_path / "state" / "vms"),
        "images": str(tmp_path / "state" / "images"),
        "firmware": str(tmp_path / "state" / "firmware"),
        "manifest": str(tmp_path / "state" / "manifest.json"),
    }
    for location in (paths["state"], paths["vms"], paths["images"], paths["firmware"]):
        Path(location).mkdir(parents=True, exist_ok=True)
    Path(paths["manifest"]).touch()
    tls = tmp_path / "agent" / "tls"
    tls.mkdir(parents=True)

    unit = host_agent.write_quadlet(
        tmp_path / "agent",
        "registry.test/agent@sha256:" + "a" * 64,
        "10.89.0.2",
        paths,
    )

    rendered = unit.read_text(encoding="utf-8")
    exec_lines = [line for line in rendered.splitlines() if line.startswith("Exec=")]
    assert len(exec_lines) == 1
    assert "--listen=0.0.0.0:9444" in exec_lines[0]
    assert f"--artifact-root={paths['vms']}" in exec_lines[0]
    assert f"--base-image-root={paths['images']}" in exec_lines[0]
    assert f"--firmware-root={paths['firmware']}" in exec_lines[0]
    assert f"--artifact-manifest={paths['manifest']}" in exec_lines[0]
    assert "--cloud-hypervisor=/opt/bin/cloud-hypervisor" in exec_lines[0]
    assert "Pull=never" in rendered
    assert "AddHost=" not in rendered
    assert f"Volume={paths['state']}:/state:rw" in rendered
    assert f"Volume={paths['images']}:/state/images:ro" in rendered
    assert f"Volume={paths['firmware']}:/state/firmware:ro" in rendered
    assert f"Volume={paths['manifest']}:/state/manifest.json:ro" in rendered
    assert f"Volume={paths['state']}:{paths['state']}:ro" in rendered
    assert f"Volume={paths['vms']}:{paths['vms']}:rw" in rendered
    assert ":/host-state/" not in rendered
    assert "ReadOnly=true" in rendered
    assert "NoNewPrivileges=true" in rendered
    assert "DropCapability=all" in rendered
    assert "/dev/kvm:/dev/kvm:rw" in rendered
    assert "Volume=/tmp/ch-capi:/tmp/ch-capi:rw" in rendered


def test_api_socket_root_must_be_owned_directory(tmp_path: Path, monkeypatch) -> None:
    socket_root = tmp_path / "ch-capi"
    monkeypatch.setattr(host_agent, "API_SOCKET_ROOT", socket_root)

    host_agent.ensure_api_socket_root()

    assert socket_root.is_dir()
    assert socket_root.stat().st_mode & 0o777 == 0o750
    socket_root.rmdir()
    socket_root.touch()
    with pytest.raises(SystemExit, match="not an owned directory"):
        host_agent.ensure_api_socket_root()


def test_agent_address_defaults_after_the_bridge_gateway(monkeypatch) -> None:
    monkeypatch.delenv("HOST_AGENT_ADDRESS", raising=False)
    monkeypatch.setattr(
        host_agent,
        "run_output",
        lambda _: json.dumps(
            [{"subnets": [{"subnet": "10.89.0.0/24", "gateway": "10.89.0.1"}]}]
        ),
    )

    assert host_agent.agent_address() == "10.89.0.2"


def test_agent_address_override_rejects_bridge_gateway(monkeypatch) -> None:
    monkeypatch.setenv("HOST_AGENT_ADDRESS", "10.89.0.1")
    monkeypatch.setattr(
        host_agent,
        "run_output",
        lambda _: json.dumps(
            [{"subnets": [{"subnet": "10.89.0.0/24", "gateway": "10.89.0.1"}]}]
        ),
    )

    with pytest.raises(SystemExit, match="non-gateway IPv4 address"):
        host_agent.agent_address()


def test_private_keys_are_not_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "private.key"
    path.touch()
    host_agent.private_path(path)

    assert os.stat(path).st_mode & 0o777 == 0o600


def test_incomplete_certificate_pair_fails_closed(tmp_path: Path) -> None:
    tls = tmp_path / "agent" / "tls"
    tls.mkdir(parents=True)
    (tls / "ca.key").touch()

    with pytest.raises(SystemExit, match="incomplete HostAgent CA certificate state"):
        host_agent.ensure_certificates(tmp_path / "agent", "10.89.0.2")


def test_ssh_key_generation_keeps_private_key_local(tmp_path: Path) -> None:
    root = tmp_path / "agent"

    def generate(command: list[str]) -> None:
        private_key = Path(command[-1])
        private_key.write_text("private", encoding="utf-8")
        private_key.with_suffix(".pub").write_text("public", encoding="utf-8")

    with patch.object(host_agent, "run", side_effect=generate):
        public_key = host_agent.ensure_ssh_key(root)

    assert public_key == root / "ssh" / "id_ed25519.pub"
    assert (root / "ssh" / "id_ed25519").stat().st_mode & 0o777 == 0o600
    assert (root / "ssh").stat().st_mode & 0o777 == 0o700


def test_incomplete_ssh_key_pair_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "agent" / "ssh"
    directory.mkdir(parents=True)
    (directory / "id_ed25519").touch()

    with pytest.raises(SystemExit, match="incomplete HostAgent SSH key state"):
        host_agent.ensure_ssh_key(tmp_path / "agent")


def test_gateway_change_rotates_only_the_server_leaf(tmp_path: Path) -> None:
    tls = tmp_path / "agent" / "tls"
    tls.mkdir(parents=True)
    for name in (
        "ca.key",
        "ca.crt",
        "server.key",
        "server.crt",
        "manager.key",
        "manager.crt",
    ):
        (tls / name).touch()

    def create_server(*_args: object) -> None:
        (tls / "server.key").touch()
        (tls / "server.crt").touch()

    with (
        patch.object(
            host_agent,
            "certificate_has_san",
            side_effect=lambda certificate, value: value == host_agent.MANAGER_IDENTITY,
        ),
        patch.object(
            host_agent, "create_server_certificate", side_effect=create_server
        ) as create,
        patch.object(host_agent, "create_manager_certificate") as create_manager,
    ):
        host_agent.ensure_certificates(tmp_path / "agent", "10.89.0.2")

    create.assert_called_once()
    create_manager.assert_not_called()


def test_host_agent_configuration_defaults_outside_the_repository() -> None:
    root = Path(__file__).parents[1]
    taskfile = (root / "Taskfile.yml").read_text(encoding="utf-8")
    ignored = (root / ".gitignore").read_text(encoding="utf-8")

    assert "HOST_AGENT_CONFIG:" in taskfile
    assert "${XDG_CONFIG_HOME:-$HOME/.config}/k8labs/host-agent" in taskfile
    assert "host-agent/" in ignored


def test_host_agent_imports_its_locked_image_before_rendering() -> None:
    root = Path(__file__).parents[1]
    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text(encoding="utf-8"))
    commands = taskfile["tasks"]["host-agent-install"]["cmds"]
    joined = "\n".join(str(entry) for entry in commands)

    assert "scripts/load_management_images.py" in joined
    assert "--image-id cluster-api-hypervisor" in joined


def test_start_replaces_an_active_quadlet(tmp_path: Path) -> None:
    unit = tmp_path / "k8labs-hypervisor-agent.container"
    unit.touch()
    (tmp_path / "settings.json").write_text(
        json.dumps({"unit": str(unit)}), encoding="utf-8"
    )
    args = type("Args", (), {"config_dir": str(tmp_path)})()

    with patch.object(host_agent, "run") as run:
        host_agent.start(args)

    assert run.call_args_list == [
        ((["systemctl", "--user", "daemon-reload"],), {}),
        (
            (
                [
                    "systemctl",
                    "--user",
                    "reload-or-restart",
                    "k8labs-hypervisor-agent.service",
                ],
            ),
            {},
        ),
    ]

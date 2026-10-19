#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from cache import validate_cache
from lock import load_lock

IMAGE_ID = "cluster-api-hypervisor"
SERVICE_NAME = "k8labs-hypervisor-agent"
LISTEN_PORT = 9444
MANAGER_IDENTITY = "spiffe://k8labs/provider/manager"
SERVER_NAME = "hypervisor-agent"
BASE_IMAGE_REFERENCE = "/host-state/images/k8labs-base.qcow2"
CUSTOM_KERNEL_IMAGE_REFERENCE = "/host-state/images/k8labs-custom-kernel.qcow2"
NEW_IMAGE_REFERENCE = CUSTOM_KERNEL_IMAGE_REFERENCE
KERNEL_IMAGE_REFERENCE = CUSTOM_KERNEL_IMAGE_REFERENCE
IMMUTABLE_IMAGE_REFERENCE = CUSTOM_KERNEL_IMAGE_REFERENCE
ROLLBACK_IMAGE_REFERENCE = BASE_IMAGE_REFERENCE
API_SOCKET_ROOT = Path("/tmp/ch-capi")


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, text=True)


def run_output(command: list[str]) -> str:
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_root(argument: str) -> Path:
    root = Path(argument).expanduser().resolve()
    if root == root.parent:
        raise ValueError("HostAgent configuration directory must not be the filesystem root")
    return root


def require_regular(path: Path, description: str) -> None:
    if not path.is_file():
        raise SystemExit(f"ERROR: {description} is not a regular file: {path}")


def ensure_api_socket_root() -> None:
    try:
        API_SOCKET_ROOT.mkdir(mode=0o750)
    except FileExistsError:
        pass
    info = API_SOCKET_ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit(f"ERROR: HostAgent API socket root is not an owned directory: {API_SOCKET_ROOT}")
    API_SOCKET_ROOT.chmod(0o750)


def agent_address() -> str:
    try:
        networks = json.loads(run_output(["podman", "network", "inspect", "kind"]))
        subnets = networks[0]["subnets"]
        ipv4 = next(
            subnet for subnet in subnets
            if ipaddress.ip_network(subnet["subnet"]).version == 4
        )
        network = ipaddress.ip_network(ipv4["subnet"])
        gateway = ipaddress.ip_address(ipv4["gateway"])
    except (KeyError, IndexError, TypeError, ValueError, StopIteration, subprocess.CalledProcessError) as error:
        raise SystemExit(
            "ERROR: unable to discover the Kind/Podman IPv4 network; create the Kind "
            "network first"
        ) from error
    override = os.environ.get("HOST_AGENT_ADDRESS")
    value = ipaddress.ip_address(override) if override else gateway + 1
    if (
        value.version != 4
        or value not in network
        or value == gateway
        or value.is_unspecified
        or value.is_loopback
        or value.is_multicast
    ):
        raise SystemExit("ERROR: HOST_AGENT_ADDRESS must be a non-gateway IPv4 address on the Kind network")
    return str(value)


def locked_image(lock: dict[str, Any]) -> str:
    for image in lock["oci_images"]:
        if image["id"] == IMAGE_ID:
            return image["reference"]
    raise SystemExit(f"ERROR: lock has no OCI image named {IMAGE_ID}")


def private_path(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def ensure_pair(key: Path, certificate: Path, description: str) -> bool:
    key_exists = key.is_file()
    certificate_exists = certificate.is_file()
    if key_exists != certificate_exists:
        raise SystemExit(
            f"ERROR: incomplete HostAgent {description} certificate state; remove both files and reinstall"
        )
    return key_exists


def certificate_has_san(certificate: Path, value: str) -> bool:
    return value in run_output([
        "openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"
    ])


def create_server_certificate(ca_key: Path, ca_cert: Path, server_key: Path, server_cert: Path, address: str) -> None:
    csr = server_key.with_suffix(".csr")
    extension = server_key.with_suffix(".ext")
    extension.write_text(
        f"subjectAltName=DNS:{SERVER_NAME},IP:{address}\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    run(["openssl", "genrsa", "-out", str(server_key), "2048"])
    run([
        "openssl", "req", "-new", "-key", str(server_key),
        "-subj", f"/CN={SERVER_NAME}", "-out", str(csr),
    ])
    run([
        "openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
        "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(server_cert),
        "-days", "365", "-sha256", "-extfile", str(extension),
    ])
    csr.unlink()
    extension.unlink()


def create_manager_certificate(ca_key: Path, ca_cert: Path, manager_key: Path, manager_cert: Path) -> None:
    csr = manager_key.with_suffix(".csr")
    extension = manager_key.with_suffix(".ext")
    extension.write_text(
        f"subjectAltName=URI:{MANAGER_IDENTITY}\nextendedKeyUsage=clientAuth\n",
        encoding="utf-8",
    )
    run(["openssl", "genrsa", "-out", str(manager_key), "2048"])
    run([
        "openssl", "req", "-new", "-key", str(manager_key),
        "-subj", "/CN=k8labs-provider-manager", "-out", str(csr),
    ])
    run([
        "openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
        "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(manager_cert),
        "-days", "365", "-sha256", "-extfile", str(extension),
    ])
    csr.unlink()
    extension.unlink()


def ensure_certificates(root: Path, address: str) -> None:
    tls = root / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    tls.chmod(stat.S_IRWXU)
    ca_key = tls / "ca.key"
    ca_cert = tls / "ca.crt"
    server_key = tls / "server.key"
    server_cert = tls / "server.crt"
    manager_key = tls / "manager.key"
    manager_cert = tls / "manager.crt"
    ca_exists = ensure_pair(ca_key, ca_cert, "CA")
    server_exists = ensure_pair(server_key, server_cert, "server")
    manager_exists = ensure_pair(manager_key, manager_cert, "manager")
    if not ca_exists:
        run(["openssl", "genrsa", "-out", str(ca_key), "4096"])
        run([
            "openssl", "req", "-x509", "-new", "-nodes", "-key", str(ca_key),
            "-sha256", "-days", "365", "-subj", "/CN=k8labs-host-agent-ca",
            "-out", str(ca_cert),
        ])
    if server_exists and not certificate_has_san(server_cert, f"IP Address:{address}"):
        server_key.unlink()
        server_cert.unlink()
        server_exists = False
    if not server_exists:
        create_server_certificate(ca_key, ca_cert, server_key, server_cert, address)
    if manager_exists and not certificate_has_san(manager_cert, MANAGER_IDENTITY):
        manager_key.unlink()
        manager_cert.unlink()
        manager_exists = False
    if not manager_exists:
        create_manager_certificate(ca_key, ca_cert, manager_key, manager_cert)
    for key in (ca_key, server_key, manager_key):
        private_path(key)


def ensure_ssh_key(root: Path) -> Path:
    directory = root / "ssh"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(stat.S_IRWXU)
    private_key = directory / "id_ed25519"
    public_key = directory / "id_ed25519.pub"
    private_exists = private_key.is_file()
    public_exists = public_key.is_file()
    if private_exists != public_exists:
        raise SystemExit(
            "ERROR: incomplete HostAgent SSH key state; remove both files and reinstall"
        )
    if not private_exists:
        run([
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "k8labs-host-agent",
            "-f", str(private_key),
        ])
    private_path(private_key)
    return public_key


def image_reference(name: str) -> str:
    """Return the manager-facing artifact key for a staged QCOW2 image."""
    return f"/host-state/images/{name}"


def discover_source_images() -> list[Path]:
    """Discover baked QCOW2 images that must be staged for rollback support."""
    build_dir = Path("build")
    if not build_dir.is_dir():
        return []
    images: list[Path] = []
    for candidate in sorted(build_dir.glob("*.qcow2")):
        if candidate.name == "fedora-cloud-base.qcow2":
            continue
        images.append(candidate.resolve())
    return images


def stage_artifacts(root: Path) -> dict[str, str]:
    source_images = discover_source_images()
    if not source_images:
        raise SystemExit("ERROR: no baked QCOW2 images found under build/")
    source_firmware = Path("build/CLOUDHV.fd").resolve()
    require_regular(source_firmware, "firmware")
    for source_image in source_images:
        require_regular(source_image, "baked base image")
    state = root / "state"
    images = state / "images"
    firmware = state / "firmware"
    vms = state / "vms"
    for directory in (images, firmware, vms):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(stat.S_IRWXU)
    targets: dict[Path, Path] = {}
    for source_image in source_images:
        targets[source_image] = images / source_image.name
    targets[source_firmware] = firmware / source_firmware.name
    for source, target in targets.items():
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, target)
    manifest_images: dict[str, dict[str, str]] = {}
    for source_image in source_images:
        target = targets[source_image]
        manifest_images[image_reference(source_image.name)] = {
            "path": str(target),
            "sha256": sha256(target),
        }
    manifest = {
        "images": manifest_images,
        "firmware": {
            "path": str(targets[source_firmware]),
            "sha256": sha256(targets[source_firmware]),
        },
    }
    manifest_path = state / "manifest.json"
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, manifest_path)
    return {
        "state": str(state),
        "images": str(images),
        "firmware": str(firmware),
        "vms": str(vms),
        "manifest": str(manifest_path),
    }


def verify_staged_artifacts(root: Path) -> list[str]:
    """Verify that every staged image hash in the manifest matches its file."""
    problems: list[str] = []
    manifest_path = root / "state" / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing staged artifact manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"invalid staged artifact manifest: {error}"]
    images = manifest.get("images")
    if not isinstance(images, dict):
        return ["staged artifact manifest has no images mapping"]
    for key, value in images.items():
        if not isinstance(value, dict):
            problems.append(f"invalid staged image entry: {key}")
            continue
        path = value.get("path")
        expected = value.get("sha256")
        if not isinstance(path, str) or not Path(path).is_file():
            problems.append(f"missing staged image for {key}: {path}")
            continue
        if not isinstance(expected, str) or sha256(Path(path)) != expected:
            problems.append(f"staged image hash mismatch for {key}: {path}")
    return problems


def write_quadlet(root: Path, image: str, address: str, paths: dict[str, str]) -> Path:
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    unit_dir = xdg_config / "containers" / "systemd"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / f"{SERVICE_NAME}.container"
    tls = root / "tls"
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    dbus = runtime_dir / "bus"
    k8netd = runtime_dir / "k8snet"
    if not dbus.exists():
        raise SystemExit(f"ERROR: required user D-Bus socket is unavailable: {dbus}")
    if not k8netd.is_dir():
        raise SystemExit(f"ERROR: required k8netd directory is unavailable: {k8netd}")
    cloud_hypervisor = shutil.which("cloud-hypervisor")
    if cloud_hypervisor is None:
        raise SystemExit("ERROR: cloud-hypervisor is required by the HostAgent")
    unit.write_text(
        "[Unit]\n"
        "Description=k8labs Hypervisor HostAgent\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Container]\n"
        f"Image={image}\n"
        "Pull=never\n"
        "Network=kind\n"
        f"IP={address}\n"
        "Entrypoint=/usr/local/bin/hypervisor-agent\n"
        "User=0\n"
        f"Volume={tls / 'server.crt'}:/tls/server/tls.crt:ro\n"
        f"Volume={tls / 'server.key'}:/tls/server/tls.key:ro\n"
        f"Volume={tls / 'ca.crt'}:/tls/ca/ca.crt:ro\n"
        f"Volume={paths['state']}:/state:rw\n"
        f"Volume={paths['images']}:/state/images:ro\n"
        f"Volume={paths['firmware']}:/state/firmware:ro\n"
        f"Volume={paths['manifest']}:/state/manifest.json:ro\n"
        "Volume=/dev/kvm:/dev/kvm:rw\n"
        f"Volume={dbus}:{dbus}:rw\n"
        f"Volume={k8netd}:{k8netd}:rw\n"
        f"Volume={API_SOCKET_ROOT}:{API_SOCKET_ROOT}:rw\n"
        f"Volume={paths['state']}:{paths['state']}:ro\n"
        f"Volume={paths['vms']}:{paths['vms']}:rw\n"
        f"Volume={xdg_config / 'systemd' / 'user'}:{xdg_config / 'systemd' / 'user'}:rw\n"
        "ReadOnly=true\n"
        "NoNewPrivileges=true\n"
        "DropCapability=all\n"
        "SecurityLabelDisable=false\n"
        f"Exec=--listen=0.0.0.0:{LISTEN_PORT} --node-id=k8labs-host "
        "--tls-cert-dir=/tls/server --client-ca=/tls/ca/ca.crt "
        "--inventory=/state/inventory.db "
        f"--user-dbus-address=unix:path={dbus} --kvm=/dev/kvm "
        f"--k8netd-socket={k8netd / 'control.sock'} "
        f"--artifact-root={paths['vms']} "
        f"--base-image-root={paths['images']} "
        f"--firmware-root={paths['firmware']} "
        f"--artifact-manifest={paths['manifest']} "
        f"--unit-dir={xdg_config / 'systemd' / 'user'} "
        f"--cloud-hypervisor={cloud_hypervisor}\n\n"
        "[Service]\n"
        "Restart=always\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    unit.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return unit


def install(args: argparse.Namespace) -> None:
    lock = load_lock(Path(args.lock))
    problems = validate_cache(lock, Path(args.cache_root).expanduser())
    if problems:
        raise SystemExit("ERROR: verified management cache required: " + "; ".join(problems))
    root = config_root(args.config_dir)
    if not Path("/dev/kvm").exists():
        raise SystemExit("ERROR: /dev/kvm is required by the HostAgent")
    address = agent_address()
    ensure_certificates(root, address)
    ssh_public_key = ensure_ssh_key(root)
    image = locked_image(lock)
    paths = stage_artifacts(root)
    ensure_api_socket_root()
    unit = write_quadlet(root, image, address, paths)
    settings = {
        "agent_address": address,
        "server_name": SERVER_NAME,
        "manager_identity": MANAGER_IDENTITY,
        "unit": str(unit),
        "tls_dir": str(root / "tls"),
        "ssh_public_key": str(ssh_public_key),
    }
    (root / "settings.json").write_text(json.dumps(settings, sort_keys=True) + "\n", encoding="utf-8")
    (root / "settings.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"rendered HostAgent service: {unit}")


def settings(root: Path) -> dict[str, str]:
    path = root / "settings.json"
    require_regular(path, "HostAgent settings")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit("ERROR: invalid HostAgent settings")
    return {str(key): str(value) for key, value in loaded.items()}


def start(args: argparse.Namespace) -> None:
    root = config_root(args.config_dir)
    configured = settings(root)
    require_regular(Path(configured["unit"]), "HostAgent Quadlet")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "reload-or-restart", f"{SERVICE_NAME}.service"])


def check(args: argparse.Namespace) -> None:
    root = config_root(args.config_dir)
    configured = settings(root)
    for required in (
        root / "tls" / "ca.crt",
        root / "tls" / "server.crt",
        root / "tls" / "server.key",
        root / "state" / "manifest.json",
    ):
        require_regular(required, "HostAgent input")
    run(["systemctl", "--user", "is-active", "--quiet", f"{SERVICE_NAME}.service"])
    print(f"HostAgent is active at {configured['agent_address']}:{LISTEN_PORT}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the separately deployed k8labs HostAgent")
    parser.add_argument("command", choices=("install", "start", "check"))
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--lock")
    parser.add_argument("--cache-root")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "install":
        if not args.lock or not args.cache_root:
            raise SystemExit("ERROR: install requires --lock and --cache-root")
        install(args)
    elif args.command == "start":
        start(args)
    else:
        check(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Fetch and verify the stock Fedora kernel RPMs pinned in versions.lock.yaml.
#
# The RPMs are not committed: they are large, immutable upstream artifacts
# identified by SHA-256. The Packer bake builds the local rpm-md metadata from
# the fetched directory.
set -euo pipefail

ROOT=$(CDPATH=; cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${VERSION:-7.3.0-65.fc46}
DEST=${DEST:-${ROOT}/build/kernel-rpms-stock/${VERSION}}
LOCK=${LOCK:-${ROOT}/versions.lock.yaml}

mkdir -p "${DEST}"
manifest=$(mktemp)
trap 'rm -f "${manifest}"' EXIT

cd "${ROOT}"
uv run python - "${LOCK}" "${VERSION}" > "${manifest}" <<'PY'
import sys
import yaml

lock_path, version = sys.argv[1], sys.argv[2]
with open(lock_path, encoding="utf-8") as handle:
    lock = yaml.safe_load(handle)

stock = lock["kernel_comparison"]["stock"]
if stock["version"] != version:
    raise SystemExit(f"lock stock kernel {stock['version']} != requested {version}")

for rpm in stock["rpms"]:
    print(f"{rpm['url']}\t{rpm['sha256']}\t{rpm['id']}")
PY

while IFS=$'\t' read -r url sha256 _id; do
    file="${DEST}/${url##*/}"
    if [[ ! -f "${file}" ]]; then
        echo "==> downloading ${url##*/}"
        curl -fL --retry 3 -o "${file}" "${url}"
    fi
    echo "${sha256}  ${file}" | sha256sum -c -
done < "${manifest}"

# The custom kernel uses the stock kernel's configuration as its base so the
# scheduler-relevant options (HZ, preemption, cgroups, BTF) match the baseline.
config="${DEST}/config-${VERSION}"
temporary="${config}.tmp"
rpm2cpio "${DEST}/kernel-core-${VERSION}.x86_64.rpm" \
    | cpio -i --to-stdout '*/config' > "${temporary}" 2>/dev/null
[[ -s "${temporary}" ]] || {
    echo "ERROR: could not extract the stock kernel config from kernel-core" >&2
    rm -f "${temporary}"
    exit 1
}
mv "${temporary}" "${config}"
echo "==> stock kernel config written to ${config}"

echo "==> stock kernel ${VERSION} ready in ${DEST}"

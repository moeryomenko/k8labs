#!/bin/bash
# Build the Rust k8s-bpf-tracer binary for release
#
# Usage:
#   ./build.sh

set -e

cd "$(dirname "$0")"

echo "Building k8s-bpf-tracer..."
cargo build --release

echo ""
echo "Build complete!"
echo "Binary: target/release/k8s-bpf-tracer"
echo "Size: $(du -h target/release/k8s-bpf-tracer | cut -f1)"
echo ""
echo "To install: sudo ./install-rust.sh"

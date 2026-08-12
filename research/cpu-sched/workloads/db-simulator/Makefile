# db-simulator — Go HTTP database workload simulator
# Build for linux/amd64. Use podman for container images.

BINARY := db-simulator
GOOS := linux
GOARCH := amd64
IMAGE_TAG ?= v1
IMAGE_NAME ?= 192.168.124.1:5000/k8labs/db-simulator
BUILDER ?= podman

.PHONY: build image push deploy clean

build: ## Build the Go binary for linux/amd64
	CGO_ENABLED=0 GOOS=$(GOOS) GOARCH=$(GOARCH) go build -o $(BINARY) .

image: build ## Build the container image (default: podman)
	$(BUILDER) build -t $(IMAGE_NAME):$(IMAGE_TAG) .

push: ## Push the image to the local registry
	$(BUILDER) push $(IMAGE_NAME):$(IMAGE_TAG)

deploy: ## Apply the deployment manifest (requires kubeconfig)
	kubectl --kubeconfig ../../../kubeconfig apply -f deploy.yaml

clean: ## Remove the binary
	rm -f $(BINARY)

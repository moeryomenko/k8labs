# cpu-burner — Go HTTP CPU burner
# Build for linux/amd64. Use podman for container images.

BINARY := cpu-burner
GOOS := linux
GOARCH := amd64
IMAGE_TAG ?= latest
IMAGE_NAME ?= cpu-burner
BUILDER ?= podman

.PHONY: build image deploy clean

build: ## Build the Go binary for linux/amd64
	CGO_ENABLED=0 GOOS=$(GOOS) GOARCH=$(GOARCH) go build -o $(BINARY) .

image: build ## Build the container image (default: podman)
	$(BUILDER) build -t $(IMAGE_NAME):$(IMAGE_TAG) .

deploy: ## Apply the deployment manifest (requires kubeconfig)
	kubectl --kubeconfig ../../../kubeconfig apply -f deploy.yaml

clean: ## Remove the binary
	rm -f $(BINARY)

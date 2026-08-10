terraform {
  required_version = ">= 1.5"

  required_providers {
    tls = {
      # The spec names the provider "community/tls", but the registry
      # provider is hashicorp/tls (the community.crypto Ansible collection is
      # the "community" namespace the spec conflated). This is the source that
      # `tofu init` resolves from registry.opentofu.org.
      #
      # Registry has no 5.x release (latest 4.3.0 as of 2026-08-10); "~> 5.4"
      # from the task description is not resolvable, so the pin is the newest
      # available major line.
      source  = "hashicorp/tls"
      version = "~> 4.3"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    local = {
      # local_file writes the rendered PKI/kubeconfig/encryption artifacts.
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

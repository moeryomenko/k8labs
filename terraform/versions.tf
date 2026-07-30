terraform {
  required_version = ">= 1.5"
  required_providers {
    cloudhypervisor = {
      source  = "registry.terraform.io/community/cloudhypervisor"
      version = "~> 0.1"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

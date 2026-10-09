terraform {
  required_version = ">= 1.8"

  required_providers {
    logfire = {
      source  = "pydantic/logfire"
      version = "~> 0.2.0"
    }
  }
}

provider "logfire" {}

variable "project_id" {
  description = "UUID of the existing bazaar-demo Logfire project."
  type        = string
}

resource "logfire_dashboard" "bazaar_strategy" {
  project_id = var.project_id
  name       = "Bazaar strategy"
  slug       = "bazaar-strategy"
  definition = file("${path.module}/bazaar-strategy.json")

  lifecycle {
    prevent_destroy = true
  }
}

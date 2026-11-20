variable "aws_region" {
  description = "AWS region where the agent Lambdas are deployed"
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "GitHub repository allowed to assume the deploy role, as owner/name"
  type        = string
}

variable "github_environment" {
  description = "GitHub Actions environment whose jobs may assume the role"
  type        = string
  default     = "production"
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider (only one is allowed per account; set false if it already exists)"
  type        = bool
  default     = false
}

variable "github_subject_prefix" {
  description = <<-EOT
    The repo part of the OIDC token's sub claim. Leave null for the classic
    format ("repo:owner/name"). Repos that use GitHub's immutable subject
    format send "repo:owner@<owner-id>/name@<repo-id>"; get the exact value with:
      gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  type        = string
  default     = null
}

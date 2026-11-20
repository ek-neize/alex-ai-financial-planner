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

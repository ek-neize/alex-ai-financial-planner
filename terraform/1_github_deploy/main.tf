# Part 1: GitHub Actions deploy role
#
# An IAM role that GitHub Actions assumes through OIDC to deploy the agent
# Lambdas' code. No long-lived AWS keys are stored in GitHub.
#
# Least privilege: the role can only upload the agent zips and update the code
# of the five agent functions. Infrastructure changes stay a manual,
# reviewed `terraform apply`.
#
# The role costs nothing, so leave this stack deployed when you tear the rest
# down; the deploy job skips itself when the Lambdas don't exist.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state in S3 (bucket and region come from ../backend.hcl):
  #   terraform init -backend-config=../backend.hcl
  backend "s3" {
    key          = "1_github_deploy/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  agents     = ["planner", "tagger", "reporter", "charter", "retirement"]

  # Must match terraform/6_agents
  lambda_packages_bucket = "alex-lambda-packages-${local.account_id}"

  # Repos using GitHub's immutable subject format send "repo:owner@id/name@id"
  github_subject_prefix = coalesce(var.github_subject_prefix, "repo:${var.github_repository}")

  github_oidc_url = "token.actions.githubusercontent.com"
  github_oidc_provider_arn = (
    var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )
}

# An account can only have one OIDC provider per URL. Reuse the existing one
# by default; set create_oidc_provider = true if your account doesn't have one.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://${local.github_oidc_url}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.github_oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "github_deploy" {
  name        = "alex-github-deploy"
  description = "Assumed by GitHub Actions (${var.github_repository}, environment ${var.github_environment}) to deploy agent Lambda code"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.github_oidc_url}:aud" = "sts.amazonaws.com"
          # Only jobs in this repo's deployment environment, not forks or other branches' PR jobs
          "${local.github_oidc_url}:sub" = "${local.github_subject_prefix}:environment:${var.github_environment}"
        }
      }
    }]
  })

  max_session_duration = 3600

  tags = {
    Project   = "alex"
    Part      = "1_github_deploy"
    ManagedBy = "terraform"
  }
}

resource "aws_iam_role_policy" "deploy_agent_code" {
  name = "deploy-agent-lambda-code"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "UploadAgentPackages"
        Effect = "Allow"
        # GetObject: UpdateFunctionCode reads the zip with the caller's permissions
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = [
          for agent in local.agents :
          "arn:aws:s3:::${local.lambda_packages_bucket}/${agent}/${agent}_lambda.zip"
        ]
      },
      {
        Sid    = "UpdateAgentFunctionCode"
        Effect = "Allow"
        Action = [
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration",
          "lambda:UpdateFunctionCode",
        ]
        Resource = [
          for agent in local.agents :
          "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:alex-${agent}"
        ]
      },
    ]
  })
}

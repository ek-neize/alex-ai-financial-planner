variable "aws_region" {
  description = "AWS region for resources"
  type        = string
}

variable "sagemaker_image_uri" {
  description = "URI of the SageMaker container image"
  type        = string
  default     = "763104351884.dkr.ecr.us-east-1.amazonaws.com/huggingface-pytorch-inference:1.13.1-transformers4.26.0-cpu-py39-ubuntu20.04"
}

variable "embedding_model_name" {
  description = "Name of the HuggingFace model to use"
  type        = string
  default     = "sentence-transformers/all-MiniLM-L6-v2"
}
variable "max_concurrency" {
  description = <<-EOT
    Max concurrent requests for the serverless embedding endpoint. Counts against the
    account quota "Maximum total concurrency that can be allocated across all serverless
    endpoints" (10 by default), shared with any other serverless endpoints. Under load,
    requests beyond this are throttled; the reporter retries with backoff.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.max_concurrency >= 1 && var.max_concurrency <= 200
    error_message = "max_concurrency must be between 1 and 200."
  }
}

output "state_bucket_name" {
  description = "Name of the Terraform state bucket"
  value       = aws_s3_bucket.state.id
}

output "backend_config" {
  description = "Contents for terraform/backend.hcl"
  value       = <<-EOT
    bucket = "${aws_s3_bucket.state.id}"
    region = "${var.aws_region}"
  EOT
}

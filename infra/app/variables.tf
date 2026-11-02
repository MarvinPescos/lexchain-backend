variable "environment" {
  type        = string
  description = "Must match the selected workspace"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "availability_zone" {
  type        = string
  default     = "us-east-1a"
  description = "Must be an AZ that offers var.instance_type"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI profile for the app account"
}

variable "gpu" {
  type        = bool
  description = "Use the GPU AMI and run Marker. Must agree with instance_type."
}

variable "instance_type" {
  type        = string
  description = "g4dn.xlarge (T4 GPU, 16 GB) with gpu = true; t3.large (8 GB) without"
}

variable "root_volume_gb" {
  type        = number
  description = "GB for Docker images, plus Marker's model weights when gpu = true"
}

variable "backup_bucket_name" {
  type        = string
  description = "bucket_name output of infra/backups for this environment"
}

variable "github_repo" {
  type        = string
  default     = "MarvinPescos/lexchain-backend"
  description = "owner/name allowed to assume the deploy role"
}

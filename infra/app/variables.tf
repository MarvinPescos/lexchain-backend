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
  description = "Must be an AZ that offers both instance types"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI profile for the app account"
}

variable "instance_type" {
  type        = string
  default     = "m7i-flex.large"
  description = "App box: everything except Marker"
}

variable "root_volume_gb" {
  type        = number
  description = "App box disk, GB for Docker images"
}

variable "marker" {
  type        = bool
  description = "Run the separate GPU box for Marker"
}

variable "marker_instance_type" {
  type        = string
  default     = "g4dn.xlarge"
  description = "Marker box; must be a GPU type"
}

variable "marker_root_volume_gb" {
  type        = number
  default     = 100
  description = "Marker box disk, GB for its image and model weights"
}

variable "backup_bucket_name" {
  type        = string
  description = "bucket_name output of infra/backups for this environment"
}

variable "backup_db_bucket_name" {
  type        = string
  description = "db_bucket_name output of infra/backups for this environment"
}

variable "backup_db_monthly_bucket_name" {
  type        = string
  description = "db_monthly_bucket_name output of infra/backups for this environment"
}

variable "github_repo" {
  type        = string
  default     = "MarvinPescos/lexchain-backend"
  description = "owner/name allowed to assume the deploy role"
}

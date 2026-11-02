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

variable "aws_profile" {
  type        = string
  description = "AWS CLI profile for the account that owns the bucket"
}

variable "bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name"
}

variable "app_account_id" {
  type        = string
  default     = null
  description = "12-digit id of the account the app runs in. Null means the same account as the bucket."

  validation {
    condition     = var.app_account_id == null || can(regex("^[0-9]{12}$", var.app_account_id))
    error_message = "app_account_id must be a 12-digit AWS account id."
  }
}

variable "retention_mode" {
  type        = string
  default     = "GOVERNANCE"
  description = "GOVERNANCE (admins can override) or COMPLIANCE (nobody can, not even root)"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.retention_mode)
    error_message = "retention_mode must be GOVERNANCE or COMPLIANCE."
  }
}

variable "retention_days" {
  type        = number
  default     = 1
  description = "How long each archived object is locked. Every object written is billed for at least this long."
}

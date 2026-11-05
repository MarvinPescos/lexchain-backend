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

variable "db_bucket_name" {
  type        = string
  description = "Globally unique name of the hourly dump bucket"
}

variable "db_monthly_bucket_name" {
  type        = string
  description = "Globally unique name of the monthly dump bucket"
}

variable "db_retention_mode" {
  type        = string
  default     = "GOVERNANCE"
  description = "Lock mode of both dump buckets. COMPLIANCE can't be shortened or removed."

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.db_retention_mode)
    error_message = "db_retention_mode must be GOVERNANCE or COMPLIANCE."
  }
}

variable "db_hourly_retention_days" {
  type        = number
  default     = 7
  description = "How long each hourly dump is locked"
}

variable "db_monthly_retention_days" {
  type        = number
  default     = 1
  description = "How long each monthly dump is locked. The firm's record-retention period once real data goes in."
}

variable "alert_email" {
  type        = string
  default     = null
  description = "Who is told when hourly dumps stop. Null means no alarm."
}

variable "rds" {
  type        = bool
  default     = false
  description = "Run the RDS copy, its bastion and its AWS Backup plan. Set false to tear them down after validation."
}

variable "rds_engine_version" {
  type        = string
  default     = "17"
  description = "Must have the same major version as Supabase"
}

variable "rds_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "rds_storage_gb" {
  type    = number
  default = 20
}

variable "rds_backup_retention_days" {
  type        = number
  default     = 3
  description = "How far back continuous backups reach"

  validation {
    condition     = var.rds_backup_retention_days >= 1 && var.rds_backup_retention_days <= 35
    error_message = "Continuous backups last 1 to 35 days."
  }
}

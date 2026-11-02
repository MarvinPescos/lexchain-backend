variable "region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI profile for the app account"
}

variable "budget_usd" {
  type        = number
  default     = 100
  description = "Monthly cost alert threshold for the whole account (staging + production)"
}

variable "alert_email" {
  type        = string
  description = "Where budget alerts go"
}

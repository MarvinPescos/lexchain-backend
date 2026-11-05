output "bucket_name" {
  value = aws_s3_bucket.backups.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.backups.arn
}

output "db_bucket_name" {
  value = aws_s3_bucket.db["hourly"].bucket
}

output "db_monthly_bucket_name" {
  value = aws_s3_bucket.db["monthly"].bucket
}

output "rds_endpoint" {
  value = one(aws_db_instance.copy[*].address)
}

output "rds_master_secret_arn" {
  value = one(aws_db_instance.copy[*].master_user_secret[0].secret_arn)
}

output "rds_bastion_id" {
  description = "aws ssm start-session --target <this>"
  value       = one(aws_instance.bastion[*].id)
}

output "rds_subnet_group" {
  value = one(aws_db_subnet_group.copy[*].name)
}

output "rds_security_group_id" {
  value = one(aws_security_group.rds[*].id)
}

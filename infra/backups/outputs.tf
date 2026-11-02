output "bucket_name" {
  value = aws_s3_bucket.backups.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.backups.arn
}

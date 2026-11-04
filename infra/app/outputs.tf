output "instance_id" {
  description = "App box, for `aws ssm start-session --target` and the deploy workflow"
  value       = aws_instance.app.id
}

output "github_deploy_role_arn" {
  description = "role-to-assume for aws-actions/configure-aws-credentials"
  value       = aws_iam_role.github_deploy.arn
}

output "marker_instance_id" {
  description = "Marker box, for SSM and the deploy workflow. null when marker = false"
  value       = one(aws_instance.marker[*].id)
}

output "marker_api_url" {
  description = "MARKER_API_URL for the app's .env. null when marker = false"
  value       = var.marker ? "http://${aws_instance.marker[0].private_ip}:${local.marker_port}" : null
}

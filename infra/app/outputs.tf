output "instance_id" {
  description = "For `aws ssm start-session --target` and the deploy workflow"
  value       = aws_instance.app.id
}

output "github_deploy_role_arn" {
  description = "role-to-assume for aws-actions/configure-aws-credentials"
  value       = aws_iam_role.github_deploy.arn
}

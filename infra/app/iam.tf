# Three identities per environment, no long-lived keys anywhere:
#   - the app instance role, which the app's containers use (SSM shell + backups)
#   - the Marker instance role (SSM shell only)
#   - the GitHub deploy role, which Actions assumes over OIDC to run a deploy

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${local.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "app_ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Write and read only. Object Lock already blocks deletes; not granting them
# keeps a compromised box from even trying.
data "aws_iam_policy_document" "app_backups" {
  statement {
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectRetention",
    ]
    resources = ["arn:aws:s3:::${var.backup_bucket_name}/*"]
  }

  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.backup_bucket_name}"]
  }

  # Add only. Restores run from the lawyer's account, so a compromised box
  # can't read past dumps.
  statement {
    actions = ["s3:PutObject"]
    resources = [
      "arn:aws:s3:::${var.backup_db_bucket_name}/*",
      "arn:aws:s3:::${var.backup_db_monthly_bucket_name}/*",
    ]
  }
}

resource "aws_iam_role_policy" "app_backups" {
  name   = "backups-write"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app_backups.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${local.name}-instance"
  role = aws_iam_role.app.name
}

# SSM only. Marker never touches backups. Created even when `marker` is off:
# it's free, and keeps toggling the box from churning IAM.
resource "aws_iam_role" "marker" {
  name               = "${local.name}-marker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "marker_ssm" {
  role       = aws_iam_role.marker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "marker" {
  name = "${local.name}-marker"
  role = aws_iam_role.marker.name
}

# Created once per account by infra/shared.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only a job that declares this GitHub Environment can deploy here. Give
    # the `production` environment a required reviewer in the repo settings
    # and a production deploy waits for your approval.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:environment:${var.environment}"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    actions = ["ssm:SendCommand"]
    resources = concat(
      [aws_instance.app.arn],
      aws_instance.marker[*].arn,
      ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"],
    )
  }

  statement {
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy-via-ssm"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

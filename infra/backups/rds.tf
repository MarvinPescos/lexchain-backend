# A copy of the app tables, because AWS point-in-time recovery only works on
# databases in AWS. Tampering replicates too: the protection is restoring to
# just before the edit. Everything here sits behind `rds` so it can be destroyed
# after validation. Setup and restore steps: RECOVERY.md.

locals {
  rds_count = var.rds ? 1 : 0
  rds_name  = "lexchain-${var.environment}-rds"
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "rds" {
  count = local.rds_count

  cidr_block                       = "10.60.0.0/16"
  assign_generated_ipv6_cidr_block = true
  enable_dns_support               = true
  enable_dns_hostnames             = true

  tags = { Name = local.rds_name }
}

# The subscription connects out to Supabase's direct host, which is IPv6-only
# by default. This route lets RDS reach it without letting anything reach RDS.
resource "aws_egress_only_internet_gateway" "rds" {
  count = local.rds_count

  vpc_id = aws_vpc.rds[0].id
}

resource "aws_internet_gateway" "rds" {
  count = local.rds_count

  vpc_id = aws_vpc.rds[0].id
}

resource "aws_subnet" "rds" {
  count = var.rds ? 2 : 0

  vpc_id            = aws_vpc.rds[0].id
  availability_zone = data.aws_availability_zones.available.names[count.index]
  cidr_block        = cidrsubnet(aws_vpc.rds[0].cidr_block, 8, count.index)
  ipv6_cidr_block   = cidrsubnet(aws_vpc.rds[0].ipv6_cidr_block, 8, count.index)

  assign_ipv6_address_on_creation = true

  tags = { Name = "${local.rds_name}-${count.index}" }
}

resource "aws_route_table" "rds" {
  count = local.rds_count

  vpc_id = aws_vpc.rds[0].id

  route {
    ipv6_cidr_block        = "::/0"
    egress_only_gateway_id = aws_egress_only_internet_gateway.rds[0].id
  }
}

resource "aws_route_table_association" "rds" {
  count = var.rds ? 2 : 0

  subnet_id      = aws_subnet.rds[count.index].id
  route_table_id = aws_route_table.rds[0].id
}

resource "aws_subnet" "bastion" {
  count = local.rds_count

  vpc_id            = aws_vpc.rds[0].id
  availability_zone = data.aws_availability_zones.available.names[0]
  cidr_block        = cidrsubnet(aws_vpc.rds[0].cidr_block, 8, 10)
  ipv6_cidr_block   = cidrsubnet(aws_vpc.rds[0].ipv6_cidr_block, 8, 10)

  assign_ipv6_address_on_creation = true

  tags = { Name = "${local.rds_name}-bastion" }
}

resource "aws_route_table" "bastion" {
  count = local.rds_count

  vpc_id = aws_vpc.rds[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.rds[0].id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.rds[0].id
  }
}

resource "aws_route_table_association" "bastion" {
  count = local.rds_count

  subnet_id      = aws_subnet.bastion[0].id
  route_table_id = aws_route_table.bastion[0].id
}

resource "aws_security_group" "bastion" {
  count = local.rds_count

  name        = "${local.rds_name}-bastion"
  description = "No inbound: reached through SSM"
  vpc_id      = aws_vpc.rds[0].id

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_security_group" "rds" {
  count = local.rds_count

  name        = local.rds_name
  description = "Postgres from the bastion; out to Supabase only"
  vpc_id      = aws_vpc.rds[0].id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.bastion[0].id]
  }

  egress {
    from_port        = 5432
    to_port          = 5432
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_db_subnet_group" "copy" {
  count = local.rds_count

  name       = local.rds_name
  subnet_ids = aws_subnet.rds[*].id
}

resource "aws_db_instance" "copy" {
  count = local.rds_count

  identifier     = local.rds_name
  engine         = "postgres"
  engine_version = var.rds_engine_version
  instance_class = var.rds_instance_class

  allocated_storage = var.rds_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "postgres"
  username = "lexchain_admin"

  # Generated and held in Secrets Manager, so it never reaches tfvars or state.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.copy[0].name
  vpc_security_group_ids = [aws_security_group.rds[0].id]
  network_type           = "DUAL"
  publicly_accessible    = false

  # Needed on for continuous backups; AWS Backup then manages the retention.
  backup_retention_period = 1

  # Only ever a copy, and the variable has to be able to remove it.
  skip_final_snapshot = true
  deletion_protection = false

  lifecycle {
    ignore_changes = [backup_retention_period, backup_window]

    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "Workspace is '${terraform.workspace}' but environment is '${var.environment}'. Run `tofu workspace select ${var.environment}` or pass the matching -var-file."
    }
  }
}

# Where the schema load, the subscription and restores are run from. RDS has no
# public endpoint, and this box has no inbound rule.
data "aws_ssm_parameter" "bastion_ami" {
  count = local.rds_count

  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bastion" {
  count = local.rds_count

  name               = "${local.rds_name}-bastion"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  count = local.rds_count

  role       = aws_iam_role.bastion[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  count = local.rds_count

  name = "${local.rds_name}-bastion"
  role = aws_iam_role.bastion[0].name
}

resource "aws_instance" "bastion" {
  count = local.rds_count

  ami                         = data.aws_ssm_parameter.bastion_ami[0].value
  instance_type               = "t4g.small"
  subnet_id                   = aws_subnet.bastion[0].id
  vpc_security_group_ids      = [aws_security_group.bastion[0].id]
  iam_instance_profile        = aws_iam_instance_profile.bastion[0].name
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required"
  }

  user_data = <<-EOT
    #!/bin/bash
    set -eu
    install -d /etc/apt/keyrings
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /etc/apt/keyrings/pgdg.asc
    . /etc/os-release
    echo "deb [signed-by=/etc/apt/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt $${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list
    apt-get update
    apt-get install -y postgresql-client-${split(".", var.rds_engine_version)[0]}
  EOT

  tags = { Name = "${local.rds_name}-bastion" }

  lifecycle {
    ignore_changes = [ami]
  }
}

# Continuous backups, in a vault locked in Governance mode: leaving out
# `changeable_for_days` is what makes it Governance. Compliance can't be changed
# after its grace period, and the vault has to go after validation.
resource "aws_backup_vault" "rds" {
  count = local.rds_count

  name = local.rds_name

  # Recovery points must not block `rds = false`.
  force_destroy = true
}

resource "aws_backup_vault_lock_configuration" "rds" {
  count = local.rds_count

  backup_vault_name  = aws_backup_vault.rds[0].name
  min_retention_days = 1
  max_retention_days = 35
}

resource "aws_backup_plan" "rds" {
  count = local.rds_count

  name = local.rds_name

  rule {
    rule_name                = "continuous"
    target_vault_name        = aws_backup_vault.rds[0].name
    schedule                 = "cron(0 * * * ? *)"
    enable_continuous_backup = true

    lifecycle {
      delete_after = var.rds_backup_retention_days
    }
  }
}

data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  count = local.rds_count

  name               = "${local.rds_name}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_assume.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  for_each = var.rds ? toset([
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
  ]) : toset([])

  role       = aws_iam_role.backup[0].name
  policy_arn = each.value
}

resource "aws_backup_selection" "rds" {
  count = local.rds_count

  name         = local.rds_name
  plan_id      = aws_backup_plan.rds[0].id
  iam_role_arn = aws_iam_role.backup[0].arn
  resources    = [aws_db_instance.copy[0].arn]
}

# Per environment: one CPU box running the compose stack (API, Celery workers,
# beat, Redis, cloudflared), plus a GPU box running only Marker when `marker`
# is on. Each environment is an OpenTofu workspace with its own state, so
# destroying staging can never touch production. Nothing here holds data that
# outlives the boxes, so `tofu destroy` is the teardown.

terraform {
  required_version = ">= 1.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project     = "lexchain"
      Component   = "app"
      Environment = var.environment
      ManagedBy   = "opentofu"
    }
  }
}

locals {
  name        = "lexchain-${var.environment}"
  marker_port = 8001

  # Docker comes from apt on plain Ubuntu; the Deep Learning AMI already has it.
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    if ! command -v docker >/dev/null; then
      apt-get update
      apt-get install -y docker.io docker-compose-v2
    fi
    systemctl enable --now docker
    usermod -aG docker ubuntu
    mkdir -p /opt/lexchain
    chown ubuntu:ubuntu /opt/lexchain
  EOT
}

# Plain Ubuntu for the app box. Ships the SSM agent.
data "aws_ssm_parameter" "app_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

# The Deep Learning Base AMI ships the NVIDIA driver, Docker, the NVIDIA
# Container Toolkit and the SSM agent.
data "aws_ssm_parameter" "marker_ami" {
  count = var.marker ? 1 : 0
  name  = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  # g4dn is not offered in every AZ; restrict to the one the user picked. Both
  # boxes share it, so app-to-Marker traffic stays free of cross-AZ charges.
  filter {
    name   = "availability-zone"
    values = [var.availability_zone]
  }
}

# No inbound rules on the app box. Traffic arrives through the Cloudflare
# Tunnel, which cloudflared opens outbound; shell access is through SSM, also
# outbound.
resource "aws_security_group" "app" {
  name        = local.name
  description = "Egress only. Ingress via Cloudflare Tunnel, shell via SSM."
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.app_ami.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  # Outbound only (tunnel, SSM, image pulls). Without a NAT gateway the box
  # needs a public address to reach them; with no inbound rules it is unreachable.
  associate_public_ip_address = true

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  # IMDSv2 only. Hop limit 2 so containers can still reach the instance role.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = local.user_data

  lifecycle {
    # A newer AMI must not silently replace a running box; recreate on purpose.
    ignore_changes = [ami]

    # Applying staging.tfvars in the production workspace (or the reverse)
    # would reshape the wrong box. Fails the plan before anything is touched.
    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "Workspace is '${terraform.workspace}' but environment is '${var.environment}'. Run `tofu workspace select ${var.environment}` or pass the matching -var-file."
    }
  }

  tags = {
    Name = local.name
  }
}

# Marker only, reached by the app box on its private IP. Its only ingress is
# the Marker port, from the app box's security group.
resource "aws_security_group" "marker" {
  count       = var.marker ? 1 : 0
  name        = "${local.name}-marker"
  description = "Marker port from the app box only. Shell via SSM."
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "marker_from_app" {
  count                        = var.marker ? 1 : 0
  security_group_id            = aws_security_group.marker[0].id
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = local.marker_port
  to_port                      = local.marker_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "marker_all" {
  count             = var.marker ? 1 : 0
  security_group_id = aws_security_group.marker[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_instance" "marker" {
  count                  = var.marker ? 1 : 0
  ami                    = data.aws_ssm_parameter.marker_ami[0].value
  instance_type          = var.marker_instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.marker[0].id]
  iam_instance_profile   = aws_iam_instance_profile.marker.name

  associate_public_ip_address = true

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.marker_root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = local.user_data

  lifecycle {
    ignore_changes = [ami]

    precondition {
      condition     = can(regex("^(g|p)[0-9]", var.marker_instance_type))
      error_message = "marker_instance_type ${var.marker_instance_type} has no GPU."
    }
  }

  tags = {
    Name = "${local.name}-marker"
  }
}

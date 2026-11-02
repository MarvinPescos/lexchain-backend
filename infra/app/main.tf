# One box per environment running the whole compose stack (API, Celery
# workers, beat, Redis, cloudflared, and Marker when `gpu` is on). Each
# environment is an OpenTofu workspace with its own state, so destroying
# staging can never touch production. Nothing here holds data that outlives
# the box, so `tofu destroy` is the teardown.

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
  name = "lexchain-${var.environment}"

  # The Deep Learning Base AMI ships the NVIDIA driver, Docker and the NVIDIA
  # Container Toolkit; plain Ubuntu has none of them and Docker is installed
  # at boot. Both ship the SSM agent.
  ami_ssm_parameter = (
    var.gpu
    ? "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
    : "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
  )
}

data "aws_ssm_parameter" "ami" {
  name = local.ami_ssm_parameter
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  # g4dn is not offered in every AZ; restrict to the one the user picked.
  filter {
    name   = "availability-zone"
    values = [var.availability_zone]
  }
}

# No inbound rules at all. Traffic arrives through the Cloudflare Tunnel, which
# cloudflared opens outbound; shell access is through SSM, also outbound.
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

# The AMI differs with and without a GPU, but the instance ignores AMI changes
# (below). This carries a `gpu` flip through as a replacement instead.
resource "terraform_data" "gpu" {
  input = var.gpu
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.ami.value
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

  lifecycle {
    # A newer AMI must not silently replace a running box; recreate on purpose.
    ignore_changes       = [ami]
    replace_triggered_by = [terraform_data.gpu]

    # Applying staging.tfvars in the production workspace (or the reverse)
    # would reshape the wrong box. Fails the plan before anything is touched.
    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "Workspace is '${terraform.workspace}' but environment is '${var.environment}'. Run `tofu workspace select ${var.environment}` or pass the matching -var-file."
    }

    precondition {
      condition     = var.gpu == can(regex("^(g|p)[0-9]", var.instance_type))
      error_message = "gpu = ${var.gpu} but instance_type ${var.instance_type} ${var.gpu ? "has no GPU" : "is a GPU type"}. Change both together."
    }
  }

  tags = {
    Name = local.name
  }
}

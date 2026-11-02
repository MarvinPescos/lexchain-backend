# infra

OpenTofu for LexChain's AWS footprint: a **staging** and a **production** environment. Why it's shaped this way: [ADR-0001](../docs/adr/0001-deploy-to-one-gpu-box-with-opentofu.md).

```
infra/
├── shared/    Account-wide: GitHub OIDC provider, budget alert. Applied once.
├── backups/   Object Lock bucket, one per environment. Applied once each, never destroyed.
└── app/       The box, its IAM, its GitHub deploy role. One per environment, destroyed when idle.
```

`backups/` and `app/` each have one **workspace per environment** (`staging`, `production`). Each workspace has its own state, so a `destroy` in staging only knows about staging. Every apply checks that the workspace matches the `environment` in the tfvars, and refuses if they differ.

## The two environments

| | staging | production |
|---|---|---|
| Instance | `t3.large` (8 GB), no GPU | `g4dn.xlarge` (16 GB, T4 GPU) |
| Marker | off (OCR via pypdf or MinerU) | on |
| AMI | Ubuntu 24.04, Docker installed at boot | Deep Learning Base GPU AMI (Ubuntu 24.04) |
| Disk | 40 GB | 100 GB |
| Cost | about $2/day | about $13/day |
| Backups bucket | `lexchain-staging-backups-…` | `lexchain-production-backups-…` |
| Deploy role trusts | GitHub Environment `staging` | GitHub Environment `production` |

To test Marker on staging, switch the three GPU lines in `staging.tfvars` (they're commented in the example), `apply`, test, then switch back and `apply` again. Flipping `gpu` **replaces** the box with a new one on the other AMI. That's fine because nothing on it is kept, but redeploy afterwards.

## One-time setup

1. **Sign in.** `aws login --profile lexchain`. The user needs EC2, IAM, S3, SSM `GetParameter` and Budgets permissions. `tofu_practice` currently has none of these.
2. **GPU quota (production).** Service Quotas → Amazon EC2 → *Running On-Demand G and VT instances* → request **4**, or **8** if staging and production will both run GPUs at once. Approval can take days. A free-plan account has to upgrade to the paid plan; credits still apply.
3. **Values.** Copy each `*.tfvars.example` to the same name without `.example` and fill it in. `*.tfvars` is gitignored.
4. **AZ.** `availability_zone` (default `us-east-1a`) must offer g4dn:
   `aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=g4dn.xlarge --region us-east-1 --profile lexchain`

## Plan and apply

**1. Shared, once per account:**

```bash
cd infra/shared
tofu init
tofu plan -out=tfplan && tofu apply tfplan
```

**2. Backups, once per environment:**

```bash
cd infra/backups
tofu init
tofu workspace new staging           # first time; afterwards: tofu workspace select staging
tofu plan -var-file=staging.tfvars -out=tfplan && tofu apply tfplan

tofu workspace new production
tofu plan -var-file=production.tfvars -out=tfplan && tofu apply tfplan
```

**3. The boxes, when you need them:**

```bash
cd infra/app
tofu init
tofu workspace new staging           # or: tofu workspace select staging
tofu plan -var-file=staging.tfvars -out=tfplan && tofu apply tfplan
tofu output                          # instance_id, github_deploy_role_arn
```

Production is the same, with `production` / `production.tfvars`. `tofu workspace show` tells you which one you're in.

After apply:

- **Shell:** `aws ssm start-session --target <instance_id> --profile lexchain`. This needs the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html). Give the box a few minutes after `apply`.
- **Tunnel:** each environment gets its own Cloudflare tunnel and hostname: `api.lexchain.me` for production, `staging-api.lexchain.me` for staging. Ingress goes to `http://backend:8000`. Two connectors on **one** tunnel share its traffic, so stop the laptop's `cloudflared` before production's takes over `lexchain-backend`.
- **GitHub:** create Environments `staging` and `production` in the repo settings. Put each `github_deploy_role_arn` and `instance_id` in that environment's secrets. Add yourself as a required reviewer on `production`.

## Tear down

```bash
cd infra/app
tofu workspace select staging
tofu destroy -var-file=staging.tfvars
```

This removes that environment's box, disk, roles and security group. Its backups bucket and the shared pieces are untouched. Postgres lives in Supabase, so no data is lost.

## Keep the environments apart

Staging must never share state with production:

| Piece | Staging | Production |
|---|---|---|
| Postgres | its own Supabase project | its own Supabase project |
| Backups | staging bucket | production bucket |
| Redis | on its box | on its box |
| Public URL | `staging-api.lexchain.me` | `api.lexchain.me` |
| Google OAuth | staging redirect URI | production redirect URI |
| Blockchain | its own contract and wallet | its own contract and wallet |
| OpenRouter | its own key | its own key |
| `.env` | its own | its own |

## Rules

- **Change infra here, not in the console.** A console edit drifts from state and the next `apply` reverts it.
- **Never commit `*.tfstate`, `terraform.tfstate.d/` or `tfplan`.** State lists every resource and can hold secrets; all of these are gitignored. State lives on the machine that ran `apply`. Lose it and OpenTofu forgets what it made, so you clean up by hand. Move it to a remote backend before anyone else runs these.
- **Commit `.terraform.lock.hcl`.** It pins the AWS provider the way `requirements.txt` pins packages.
- **COMPLIANCE retention is irreversible.** Every object stays, and is billed, for `retention_days`, and nobody can delete it, root included. Keep `GOVERNANCE` / 1 day until this is real production.
- **Buckets have `prevent_destroy`.** `tofu destroy` in `backups/` refuses on purpose.

## Not yet wired

- The app has no S3 backup provider; `BACKUP_PROVIDER` is still `supabase`. It needs `boto3`, and Object Lock rejects a `PutObject` without a checksum, so send `ChecksumAlgorithm`.
- No `docker-compose.prod.yml` (GPU Marker service, cloudflared, no bind mounts or `--reload`) and no `.github/workflows/`. Each box comes up with Docker running and an empty `/opt/lexchain`.

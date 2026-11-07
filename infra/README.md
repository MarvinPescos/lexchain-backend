# infra

OpenTofu for LexChain's AWS footprint: a **staging** and a **production** environment. Why it's shaped this way: [ADR-0001](../docs/adr/0001-deploy-to-one-gpu-box-with-opentofu.md).

```
infra/
├── shared/    Account-wide: GitHub OIDC provider, budget alert. Applied once.
├── backups/   Object Lock buckets (documents, hourly and monthly dumps), the missing-dump
│              alarm and the optional RDS copy. Applied once per environment; the buckets
│              are never destroyed. Restores: backups/RECOVERY.md.
└── app/       The app box, the optional Marker GPU box, their IAM, the GitHub deploy role.
               One per environment, destroyed when idle.
```

`backups/` and `app/` each have one **workspace per environment** (`staging`, `production`). Each workspace has its own state, so a `destroy` in staging only knows about staging. Every apply checks that the workspace matches the `environment` in the tfvars, and refuses if they differ.

## The two environments

| | staging | production |
|---|---|---|
| App box | `m7i-flex.large` (2 vCPU, 8 GB), Ubuntu 24.04, 40 GB | same |
| Runs on the app box | API, workers, beat, Redis, cloudflared | same |
| OCR | MinerU on Azure (outside this repo's infra) | Marker on its own box |
| Marker box | off (`marker = true` to test) | `g4dn.xlarge` (T4 GPU, 16 GB), Deep Learning Base GPU AMI, 100 GB |
| Cost | about $2.50/day | about $15/day (app ~$2.50 + Marker ~$12.60) |
| Backups bucket | `lexchain-staging-backups-…` | `lexchain-production-backups-…` |
| Deploy role trusts | GitHub Environment `staging` | GitHub Environment `production` |

The app reaches Marker over the private network: set `MARKER_API_URL` in that environment's `.env` to the `marker_api_url` output (`http://<private-ip>:8001`). The Marker box accepts that port only from the app box's security group and has no other inbound rule. Its private IP changes whenever it's recreated, so update `.env` after any replace.

To test Marker on staging, set `marker = true` in `staging.tfvars`, `apply`, deploy the OCR service to the new box, point `MARKER_API_URL` at it and set `OCR_ENGINE=marker`, test, then set `marker = false` and `apply` again. Toggling `marker` only creates or destroys the Marker box; the app box is untouched.

## One-time setup

1. **Sign in.** `aws login --profile lexchain`. The user needs EC2, IAM, S3, SSM `GetParameter` and Budgets permissions. `tofu_practice` currently has none of these.
2. **GPU quota (Marker box).** Service Quotas → Amazon EC2 → *Running On-Demand G and VT instances* → request **4**, or **8** if staging and production will both run Marker at once. Approval can take days. A free-plan account has to upgrade to the paid plan; credits still apply.
3. **Values.** Copy each `*.tfvars.example` to the same name without `.example` and fill it in. `*.tfvars` is gitignored.
4. **AZ.** `availability_zone` (default `us-east-1a`) must offer both instance types:
   `aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=g4dn.xlarge,m7i-flex.large --region us-east-1 --profile lexchain`

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
tofu output                          # instance_id, marker_instance_id, marker_api_url, github_deploy_role_arn
```

Production is the same, with `production` / `production.tfvars`. `tofu workspace show` tells you which one you're in.

After apply:

- **Shell:** `aws ssm start-session --target <instance_id> --profile lexchain` (or `<marker_instance_id>` for the Marker box). This needs the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html). Give the box a few minutes after `apply`.
- **Tunnel:** each environment gets its own Cloudflare tunnel and hostname: `api.lexchain.me` for production, `staging-api.lexchain.me` for staging. Ingress goes to `http://backend:8000`. Two connectors on **one** tunnel share its traffic, so stop the laptop's `cloudflared` before production's takes over `lexchain-backend`.
- **GitHub:** create Environments `staging` and `production` in the repo settings. Put each `github_deploy_role_arn`, `instance_id` and (production) `marker_instance_id` in that environment's secrets. The deploy role may run commands on both boxes. Add yourself as a required reviewer on `production`.

## Deploy

A push to `main` builds the image, pushes it to GHCR and rolls it out on the staging box over SSM. It fails if the API doesn't come up healthy.

Before the first push, put that environment's `.env` at `/opt/lexchain/.env` on the box. It needs `DB_URL` and `TUNNEL_TOKEN` on top of the usual values. The workflow doesn't touch it, so a change there needs a re-run of the deploy.

## Tear down

```bash
cd infra/app
tofu workspace select staging
tofu destroy -var-file=staging.tfvars
```

This removes that environment's boxes, disks, roles and security groups. Its backups bucket and the shared pieces are untouched. Postgres lives in Supabase, so no data is lost.

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

- `BACKUP_PROVIDER` defaults to `supabase`. Set `s3` and `BACKUP_BUCKET` once the backups bucket exists.
- No compose file for the Marker box (`ocr-service` with the GPU). Each box comes up with Docker running and an empty `/opt/lexchain`.
- `.github/workflows/deploy.yml` only targets staging.

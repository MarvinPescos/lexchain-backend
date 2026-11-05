# Database recovery runbook

Run all of this from the lawyer's admin account in the backups account. The app server can add dumps but can't read them, by design.

| Layer | Worst-case loss | Kept for | Covers |
|---|---|---|---|
| RDS copy with AWS Backup continuous backups | About 5 minutes plus replication lag | Up to 35 days | App tables |
| Hourly `pg_dump` | 1 hour | 7 days | Everything, including `auth` |
| Monthly `pg_dump` | 1 month | The firm's retention period | Everything, including `auth`. For tampering noticed late |

Finalized PDFs and their anchors lose nothing, because both are written at finalize.

## Turn on the dumps

1. Apply `infra/backups` and `infra/app`. Confirm the SNS subscription email.
2. Check the server version with `select version();` in Supabase. If its major is above 17, build the image with `--build-arg PG_CLIENT_MAJOR=<major>`.
3. Set these in the server's `.env`, then start the `celery-backups` worker:

   ```
   BACKUP_DB_URL=postgresql://postgres.<ref>:<password>@<region>.pooler.supabase.com:5432/postgres
   BACKUP_DB_BUCKET=<db_bucket_name output>
   BACKUP_DB_MONTHLY_BUCKET=<db_monthly_bucket_name output>
   AWS_REGION=ap-southeast-1
   ```

   Use the session pooler (port 5432). The transaction pooler doesn't work with `pg_dump`, and the direct host is IPv6-only by default.
4. Wait for `backup.db.stored` in the worker log, then confirm a `.sql.gz` in the hourly bucket.

Every object written to the documents bucket must send `If-None-Match`, admins included. For the plan's section 7 row 7 test, pass `--if-none-match '*'` to `aws s3api put-object`.

## Restore a dump

Use a **fresh Supabase project**. A local Postgres lacks Supabase's `auth` and `storage` roles and schemas, so it proves nothing.

1. Download the dump from the hourly bucket, or from the monthly bucket for tampering noticed late.
   Use a `psql` at least as new as the `pg_dump` that wrote it. Older clients reject the `\restrict` line current dumps start with.
2. Load it, keeping the output:

   ```bash
   gunzip -c <dump>.sql.gz | psql "<new project session pooler URL>" 2>&1 | tee restore.log
   ```

   The new project already has Supabase's own objects, so expect `already exists` errors for those. Read the rest of the log.
3. Compare table row counts with the source, and sign in with a real user.
4. Run the chain reconcile job. Documents finalized after the dump have an archive and an anchor but no database row, and it lists them. Re-enter those from the archive.
5. Note the time taken.

## Set up the RDS copy

Set `rds = true` in the tfvars and apply. Check that the AWS Backup service opt-in lists RDS as enabled (AWS Backup console, Settings).

Run the rest from the bastion: `aws ssm start-session --target <rds_bastion_id output>`. The RDS master password is in Secrets Manager, under `rds_master_secret_arn`.

1. **Test the network first.** Private dual-stack RDS endpoints are VPC-only, and whether RDS can open an outbound IPv6 connection to Supabase hasn't been tested. The subscription in step 4 is the test. If it can't connect, buy Supabase's IPv4 add-on and give the RDS subnets a NAT gateway.
2. In Supabase, create the replication role and publication:

   ```sql
   create role rds_replication with login replication password '<password>';
   grant usage on schema public to rds_replication;
   grant select on all tables in schema public to rds_replication;
   alter default privileges in schema public grant select on tables to rds_replication;
   create publication rds_copy for tables in schema public;
   ```

   `auth` stays out; dumps cover it.
3. Load the schema into RDS:

   ```bash
   pg_dump --schema-only --schema=public --no-owner --no-privileges "<session pooler URL>" > schema.sql
   psql "<rds url>" -c 'create extension if not exists vector;'
   psql "<rds url>" -f schema.sql
   ```

4. Subscribe, using Supabase's **direct** connection, because subscriptions don't work through the pooler:

   ```sql
   create subscription rds_copy
     connection 'host=db.<ref>.supabase.co port=5432 dbname=postgres user=rds_replication password=<password> sslmode=require'
     publication rds_copy;
   ```

5. Set `BACKUP_REPLICATION_SLOT=rds_copy` and `BACKUP_ALERT_EMAIL=<address>` on the server. The worker then emails that address if the subscription stops or falls behind.

**Every Alembic migration also has to run on RDS**, because schema changes don't replicate. Run it there first, so replication doesn't stop on a missing column. A new table also needs `alter subscription rds_copy refresh publication;`.

## Restore RDS to just before an edit

1. Find the time of the edit, from verification history or the audit log.
2. Restore to a new instance, a minute before it:

   ```bash
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier lexchain-<environment>-rds \
     --target-db-instance-identifier lexchain-<environment>-rds-restored \
     --restore-time <UTC time> \
     --db-subnet-group-name <rds_subnet_group output> \
     --vpc-security-group-ids <rds_security_group_id output> \
     --no-publicly-accessible
   ```

3. Check the tampered value is gone from the restored instance.
4. From the bastion, copy `public` back into Supabase:

   ```bash
   pg_dump --data-only --schema=public --no-owner "<restored rds url>" > public.sql
   ```

   Stop the workers, truncate the app tables in Supabase, and load `public.sql` with `session_replication_role = replica`, so foreign keys don't depend on load order.
5. Disable the subscription on the old instance and delete the restored one when done. The slot on Supabase is still in use, so drop the subscription first if RDS is being rebuilt.
6. Note the time taken and the rows lost.

## Tear down after validation

1. Set `rds = false` and apply. If the vault lock refuses to delete recovery points younger than `min_retention_days`, wait a day and apply again.
2. In Supabase, run `drop publication rds_copy; drop role rds_replication;`, and check `pg_replication_slots` is empty. **A slot left behind holds WAL until the disk fills.**
3. Remove `BACKUP_REPLICATION_SLOT` from the server.
4. Check that nothing else is billing in either account.

The dump buckets stay: they have `prevent_destroy`, and their locks are short during validation.

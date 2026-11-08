# Database recovery runbook

Run all of this from the lawyer's admin account in the backups account. The app server can add dumps but can't read them, by design.

| Layer | Worst-case loss | Kept for | Covers |
|---|---|---|---|
| RDS copy with AWS Backup continuous backups | About 5 minutes plus replication lag | Up to 35 days | App tables |
| Hourly `pg_dump` | 1 hour | 7 days | Everything, including `auth` |
| Monthly `pg_dump` (every dump of the 1st) | 1 month | The firm's retention period | Everything, including `auth`. For tampering noticed late |

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
   BACKUP_ALERT_EMAIL=<address>
   ```

   Use the session pooler (port 5432). The transaction pooler doesn't work with `pg_dump`, and the direct host is IPv6-only by default.
4. Wait for `backup.db.stored` in the worker log, then confirm a `.sql.gz` in the hourly bucket.

Every object written to the documents and dump buckets must send `If-None-Match`, admins included. For the plan's section 7 row 7 test, pass `--if-none-match '*'` to `aws s3api put-object`.

## Restore a dump

Use a **fresh Supabase project**. A local Postgres lacks Supabase's `auth` and `storage` roles and schemas, so it proves nothing.

1. Download the dump from the hourly bucket, or from the monthly bucket for tampering noticed late.
   Use a `psql` at least as new as the `pg_dump` that wrote it. Older clients reject the `\restrict` line current dumps start with.
2. Load it, keeping the output:

   ```bash
   gunzip -c <dump>.sql.gz | psql "<new project session pooler URL>" 2>&1 | tee restore.log
   ```

   The new project already has Supabase's own objects, so expect `already exists`, `permission denied` and `must be owner` errors for those. Read the rest of the log.

   `auth.identities`, `sessions` and `refresh_tokens` don't load: they come before `auth.users` and fail their foreign keys. Users sign in again, and email sign-in works without `identities`. Other providers weren't tested and would likely need those rows back.
3. Compare table row counts with the source, and sign in with a real user.
4. Run the chain reconcile job. Documents finalized after the dump have an archive and an anchor but no database row, and it lists them. Re-enter those from the archive.
5. If `lifecycle`, the snapshot or the metadata was edited on a document, its Restore action repairs them with the file, from the chain and the archive. Nothing needs fixing by hand first.
6. Note the time taken.

## Set up the RDS copy

Set `rds = true` in the tfvars and apply. Check that the AWS Backup service opt-in lists RDS as enabled (AWS Backup console, Settings).

Run the rest from the bastion: `aws ssm start-session --target <rds_bastion_id output>`. The RDS master password is in Secrets Manager, under `rds_master_secret_arn`.

1. **Sort out the network first.** Private dual-stack RDS can't reach Supabase's IPv6-only direct host, so the subscription in step 4 times out. For production, buy Supabase's IPv4 add-on and give the RDS subnets a NAT gateway. The validation run used a `socat` relay on the bastion instead, which is fine for a test but not for keeping.
2. In Supabase, create the replication role and publication:

   ```sql
   create role rds_replication with login replication password '<password>';
   grant usage on schema public to rds_replication;
   grant select on all tables in schema public to rds_replication;
   alter default privileges in schema public grant select on tables to rds_replication;
   do $$ begin
     execute (
       select 'create publication rds_copy for table '
         || string_agg(format('%I.%I', schemaname, tablename), ', ')
       from pg_tables where schemaname = 'public'
     );
   end $$;
   ```

   The tables are listed because `for tables in schema` needs a superuser, which Supabase's `postgres` role isn't. `auth` stays out; dumps cover it.
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

5. Set `BACKUP_REPLICATION_SLOT=rds_copy` on the server. The worker then emails `BACKUP_ALERT_EMAIL` if the subscription stops or falls behind. Lag is measured in bytes of WAL, so a quiet database can sit behind for a long time without an alert, with little to lose.

**Every Alembic migration also has to run on RDS**, because schema changes don't replicate. Add columns on RDS first and drop them on RDS last, so replication never meets a column it doesn't have. A new table also needs `alter publication rds_copy add table <table>;` in Supabase, then `alter subscription rds_copy refresh publication;` on RDS.

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

3. **Detach the restored instance from Supabase straight away.** It carries the same subscription, and if it connects it takes over the slot and pulls the tampered rows back in. `slot_name = none` keeps the drop from removing the slot the live copy uses:

   ```sql
   alter subscription rds_copy disable;
   alter subscription rds_copy set (slot_name = none);
   drop subscription rds_copy;
   ```

4. Check the tampered value is gone from the restored instance.
5. From the bastion, copy `public` back into Supabase:

   ```bash
   pg_dump --data-only --schema=public --no-owner "<restored rds url>" > public.sql
   ```

   Stop the workers, truncate the app tables in Supabase, and load `public.sql` with `session_replication_role = replica`, so foreign keys don't depend on load order. Run the truncate and the load in one transaction (`psql -1 -v ON_ERROR_STOP=1`), so a failed load leaves the data as it was.
6. The load replicates to the live copy, so it ends up matching. Delete the restored instance when done, and before any teardown: it uses the same subnet group and security group.
7. Note the time taken and the rows lost.

## Tear down after validation

1. Remove `BACKUP_REPLICATION_SLOT` from the server.
2. On RDS, run `drop subscription rds_copy;`. This also drops the slot on Supabase.
3. Set `rds = false` and apply. If the vault refuses to delete a recovery point, delete that recovery point by hand in the AWS Backup console and apply again.
4. In Supabase, clean up:

   ```sql
   drop publication rds_copy;
   grant rds_replication to current_user;
   drop owned by rds_replication;
   drop role rds_replication;
   select slot_name from pg_replication_slots;
   ```

   **A slot left behind holds WAL until the disk fills.** If `rds_copy` is still listed, run `select pg_drop_replication_slot('rds_copy');`.
5. Check that nothing else is billing in either account.

The dump buckets stay: they have `prevent_destroy`, and their locks are short during validation.

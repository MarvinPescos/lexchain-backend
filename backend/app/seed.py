"""
Seed script — creates initial lawyer and super admin accounts.

Usage (inside Docker):
    docker compose exec backend python -m app.seeds

Usage (local venv):
    python -m app.seeds
"""

import asyncio

import app.all_models  # noqa: F401 — registers all SQLAlchemy models
from app.infrastructure.clients.supabase_client import get_supabase_admin
from app.infrastructure.database.sessions import AsyncSessionLocal
from app.features.users.repository import UserRepository

# ── Seed data ────────────────────────────────────────────────────────────────

SEED_USERS = [
    {
        "email": "superadmin@lexchain.com",
        "password": "SuperAdmin123!",
        "f_name": "Super",
        "l_name": "Admin",
        "role": "super_admin",
    },
    {
        "email": "lawyer@lexchain.com",
        "password": "Lawyer123!",
        "f_name": "Test",
        "l_name": "Lawyer",
        "role": "lawyer",
    },
]

# ── Seeder ───────────────────────────────────────────────────────────────────

async def seed() -> None:
    supabase = get_supabase_admin()

    async with AsyncSessionLocal() as session:
        repo = UserRepository(session)

        for data in SEED_USERS:
            print(f"  Seeding {data['role']}: {data['email']} ...", end=" ")

            # Check if already exists locally to avoid duplicate seed runs
            existing = await repo.get_by_email(data["email"])
            if existing:
                print("already exists, skipped.")
                continue

            # Step 1: Create in Supabase Auth — skips email confirmation
            auth_response = supabase.auth.admin.create_user(
                {
                    "email": data["email"],
                    "password": data["password"],
                    "email_confirm": True,
                }
            )

            if not auth_response.user:
                print(f"FAILED — Supabase did not return a user.")
                continue

            # Step 2: Sync to local DB (role defaults to "user" inside sync_from_supabase)
            user = await repo.sync_from_supabase(
                supabase_user_id=auth_response.user.id,
                email=data["email"],
                f_name=data["f_name"],
                l_name=data["l_name"],
                is_email_verified=True,
            )

            # Step 3: Force the correct role (sync_from_supabase defaults to "user")
            await repo.update(user, role=data["role"])

            print("✓ done.")

        await session.commit()

    print("\nSeeding complete.")


if __name__ == "__main__":
    asyncio.run(seed())

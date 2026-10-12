"""
Seed script — creates the lawyer account, a client for testing sharing, and the
lawyer's first notarial register (book).

Idempotent: safe to re-run after wiping the database. Supabase Auth accounts
survive a database drop, so an existing auth user is reused rather than treated
as an error.

Usage (inside Docker):
    docker compose exec backend python -m app.seed

Usage (local venv):
    python -m app.seed
"""

import asyncio

import app.all_models  # noqa: F401 — registers all SQLAlchemy models
from app.infrastructure.clients.supabase_client import get_supabase_admin
from app.infrastructure.database.sessions import AsyncSessionLocal
from app.features.books.repository import BookRepository
from app.features.users.repository import UserRepository

# ── Seed data ────────────────────────────────────────────────────────────────
# Admin and lawyer are one role now, so there is no separate super admin. The
# client exists so document sharing and the invitation flow can be demonstrated.

SEED_USERS = [
    {
        "email": "lawyer@lexchain.com",
        "password": "Lawyer123!",
        "f_name": "Test",
        "l_name": "Lawyer",
        "role": "lawyer",
    },
    {
        "email": "client@lexchain.com",
        "password": "Client123!",
        "f_name": "Test",
        "l_name": "Client",
        "role": "user",
    },
]

# The notarial register documents are filed under. Uploads require a book_id,
# so without this the system cannot accept a document at all.
SEED_BOOK = {"book_number": 1, "series_year": 2026}

# ── Seeder ───────────────────────────────────────────────────────────────────


def _find_auth_user(supabase, email: str):
    """Look up an existing Supabase Auth account by email.

    Auth lives outside the application database, so it outlives a schema drop.
    Re-seeding must reuse those accounts instead of trying to create duplicates.
    """
    try:
        page = supabase.auth.admin.list_users()
        users = page if isinstance(page, list) else getattr(page, "users", [])
        for u in users:
            if (getattr(u, "email", "") or "").lower() == email.lower():
                return u
    except Exception as e:
        print(f"(could not list auth users: {e})", end=" ")
    return None


async def seed() -> None:
    supabase = get_supabase_admin()

    async with AsyncSessionLocal() as session:
        repo = UserRepository(session)

        for data in SEED_USERS:
            print(f"  {data['role']:<7} {data['email']} ...", end=" ")

            existing = await repo.get_by_email(data["email"])
            if existing:
                # Re-assert the role: a row recreated by a normal sign-in after a
                # wipe defaults to "user", which would lock the lawyer out.
                if existing.role != data["role"]:
                    await repo.update(existing, role=data["role"])
                    print(f"exists, role corrected to {data['role']}.")
                else:
                    print("already seeded, skipped.")
                continue

            auth_user = None
            try:
                created = supabase.auth.admin.create_user(
                    {
                        "email": data["email"],
                        "password": data["password"],
                        "email_confirm": True,
                    }
                )
                auth_user = created.user
            except Exception:
                # Almost always "email already registered" — the account survived
                # a database wipe. Reuse it.
                auth_user = _find_auth_user(supabase, data["email"])

            if not auth_user:
                print("FAILED — no Supabase auth user available.")
                continue

            user = await repo.sync_from_supabase(
                supabase_user_id=auth_user.id,
                email=data["email"],
                f_name=data["f_name"],
                l_name=data["l_name"],
                is_email_verified=True,
            )

            # sync_from_supabase defaults new users to "user"; force the seeded role.
            await repo.update(user, role=data["role"])
            print("done.")

        await session.commit()

        # The book belongs to the lawyer, so it is seeded after the accounts.
        lawyer = await repo.get_by_email("lawyer@lexchain.com")
        if lawyer:
            books = BookRepository(session)
            existing_book = await books.get_by_number_and_year(
                lawyer.id, SEED_BOOK["book_number"], SEED_BOOK["series_year"]
            )
            label = f"Book {SEED_BOOK['book_number']}, Series of {SEED_BOOK['series_year']}"
            print(f"  book    {label} ...", end=" ")
            if existing_book:
                print(f"already seeded, id={existing_book.id}")
            else:
                book = await books.create(user_id=lawyer.id, **SEED_BOOK)
                await session.commit()
                print(f"done. id={book.id}")

    print("\nSeeding complete.")


if __name__ == "__main__":
    asyncio.run(seed())

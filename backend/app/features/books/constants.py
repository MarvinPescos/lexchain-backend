# === Book status ===
# OPEN   -> the register currently in use for its year; at most one per year
# CLOSED -> a finished register: closed by the lawyer, filled to its last page,
#           or created closed to migrate a physical book that is already done
BOOK_OPEN = "OPEN"
BOOK_CLOSED = "CLOSED"

BOOK_STATUSES = (BOOK_OPEN, BOOK_CLOSED)

# === Register capacity ===
# A notarial register book has 106 pages with up to 5 entries per page.
MAX_PAGES = 106
MAX_ENTRIES_PER_PAGE = 5
MAX_ENTRIES = MAX_PAGES * MAX_ENTRIES_PER_PAGE  # 530 entries per book

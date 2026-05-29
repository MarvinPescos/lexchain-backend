from pydantic import BeforeValidator
from typing import Annotated
import re

def validate_name(name: str, label: str = "Name") -> str:
    """
    Validates first or last names: 
    - 1-50 characters
    - Allows letters, hyphens, spaces, and apostrophes
    - Strips whitespace and capitalizes correctly
    """
    name = name.strip()
    if len(name) < 1:
        raise ValueError(f"{label} cannot be empty")
    if len(name) > 50:
        raise ValueError(f"{label} is too long (max 50 characters)")
    
    # Allows Unicode letters (for accents), spaces, hyphens, and apostrophes
    # Note: \w includes underscores, so we use a more specific character class
    if not re.match(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-\']+$", name):
        raise ValueError(f"{label} contains invalid characters")
        
    # Upercase first letter/char
    return name.title()

def validate_password(password: str) -> str:
    """
    Validates password: 
    - min 8 characters
    - must include uppercase, lowercase, and a digit
    """
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    if not re.search(r'[A-Z]', password):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r'[a-z]', password):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r'\d', password):
        raise ValueError("Password must contain at least one digit")
    return password

# Makes it optional
def validate_phone_number(phone_number: str | None) -> str | None:
    """
    - Validates phone number (Optional):
    - Must only contain digits, optional leading '+', and spaces or dashes
    - If provided, must be between 7 and 15 digits (standard international length)
    """

    if not phone_number or not phone_number.strip():
        return None
    
    phone_number = phone_number.strip()

    if not re.match(r"^\+?[0-9]{7,15}$", phone_number.replace(" ", "").replace("-", "")):
        raise ValueError("Invalid phone number format. Use numeric format (e.g., +639123456789)")

    return phone_number

ValidatedPhone = Annotated[str | None, BeforeValidator(validate_phone_number)]
ValidatedName = Annotated[str, BeforeValidator(validate_name)]
ValidatedPassword = Annotated[str, BeforeValidator(validate_password)]

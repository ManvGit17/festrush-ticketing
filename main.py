"""Entry point kept at the project root so ``uvicorn main:app`` keeps working as in v1.

The application itself lives in ``src/festrush``. The original v1 code is preserved unchanged in
``legacy/v1``.
"""

from festrush.app import create_app

app = create_app()
SRN = "PES1UG23CS349"

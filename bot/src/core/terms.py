"""Single source of truth for the current legal-document version.

Acceptance stored on a user (``accepted_terms_version``) is only valid when it
equals :data:`TERMS_VERSION`. Bumping this constant re-prompts every user with
the acceptance gate on their next interaction.
"""

# Bump this when the Privacy Policy or the Terms of Service materially changes.
TERMS_VERSION = "2026-06-22"

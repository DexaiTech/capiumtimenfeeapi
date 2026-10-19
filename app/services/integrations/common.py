"""What every integration gateway shares: the integration keys and the two ways a call can fail.

Each integration is one interface (a ``Protocol``) with a mock implementation, selected by a
setting (``*_GATEWAY``); a real gateway is one more class with the same methods. The services
catch these two errors and decide what a failure means for their own work (a retry later, a
failed item for a person to look at); nothing here ever reaches a billing or time transaction.
"""

CENTRAL_CLIENT = "central_client"
CENTRAL_USER = "central_user"
PRACTICE_MANAGEMENT = "practice_management"
BOOKKEEPING = "bookkeeping"
INTEGRATIONS = (CENTRAL_CLIENT, CENTRAL_USER, PRACTICE_MANAGEMENT, BOOKKEEPING)

DEFAULT_CHANGE_BATCH = 100


class IntegrationUnavailableError(Exception):
    """The other system cannot be reached or answered with a failure: the same request may
    succeed later, so it is retried with a backoff."""


class IntegrationRejectedError(Exception):
    """The other system refused this request for good (an unknown job, a document that does not
    add up): repeating it cannot succeed, so it fails at once and waits for a person."""

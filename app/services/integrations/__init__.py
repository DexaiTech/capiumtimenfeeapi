"""The integrations as the rest of the application sees them: one interface and one mock each."""

from app.services.integrations.bookkeeping import (
    BookkeepingDocument,
    BookkeepingGateway,
    BookkeepingLine,
    BookkeepingReceipt,
    MockBookkeepingGateway,
)
from app.services.integrations.central_user import (
    CentralUserChanges,
    CentralUserEvent,
    CentralUserGateway,
    MockCentralUserGateway,
)
from app.services.integrations.common import (
    BOOKKEEPING,
    CENTRAL_CLIENT,
    CENTRAL_USER,
    INTEGRATIONS,
    PRACTICE_MANAGEMENT,
    IntegrationRejectedError,
    IntegrationUnavailableError,
)
from app.services.integrations.factory import (
    get_bookkeeping_gateway,
    get_central_user_gateway,
    get_practice_management_gateway,
    get_subscription_gateway,
    reset_integration_gateways,
)
from app.services.integrations.practice_management import (
    MockPracticeManagementGateway,
    PmChanges,
    PmEvent,
    PmJob,
    PmJobPage,
    PmReceipt,
    PmTask,
    PracticeManagementGateway,
)
from app.services.integrations.subscription import (
    MockSubscriptionGateway,
    SubscriptionGateway,
    SubscriptionInfo,
    SubscriptionStatus,
)

__all__ = [
    "BOOKKEEPING",
    "CENTRAL_CLIENT",
    "CENTRAL_USER",
    "INTEGRATIONS",
    "PRACTICE_MANAGEMENT",
    "BookkeepingDocument",
    "BookkeepingGateway",
    "BookkeepingLine",
    "BookkeepingReceipt",
    "CentralUserChanges",
    "CentralUserEvent",
    "CentralUserGateway",
    "IntegrationRejectedError",
    "IntegrationUnavailableError",
    "MockBookkeepingGateway",
    "MockCentralUserGateway",
    "MockPracticeManagementGateway",
    "MockSubscriptionGateway",
    "PmChanges",
    "PmEvent",
    "PmJob",
    "PmJobPage",
    "PmReceipt",
    "PmTask",
    "PracticeManagementGateway",
    "SubscriptionGateway",
    "SubscriptionInfo",
    "SubscriptionStatus",
    "get_bookkeeping_gateway",
    "get_central_user_gateway",
    "get_practice_management_gateway",
    "get_subscription_gateway",
    "reset_integration_gateways",
]

"""
Mattermost Auth Manager - Maps Mattermost user identity to RBAC roles.

Reads user->role mappings from services.mattermost.auth config.
Config structure:
    services:
      mattermost:
        auth:
          enabled: true
          default_role: base-user
          user_roles:
            jsmith: [archi-expert]
            user_id_abc123: [archi-admins]
"""

from typing import Dict, List

from src.utils.rbac.mattermost_context import MattermostUserContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


class MattermostAuthManager:
    """
    Resolves Mattermost users to RBAC roles.

    When auth is disabled, all users receive the default_role.
    When enabled, looks up username first, then user_id in user_roles config.
    Falls back to default_role for unknown users.
    """

    def __init__(self, auth_config: dict):
        self.enabled: bool = auth_config.get('enabled', False)
        self.default_role: str = auth_config.get('default_role', 'base-user')
        self.user_roles: Dict[str, List[str]] = auth_config.get('user_roles', {})

        if self.enabled:
            logger.info(
                f"MattermostAuthManager: enabled=True, "
                f"default_role={self.default_role!r}, "
                f"{len(self.user_roles)} user role mapping(s)"
            )
        else:
            logger.info(
                f"MattermostAuthManager: disabled — all users get "
                f"default_role={self.default_role!r}"
            )

    def get_roles(self, username: str, user_id: str) -> List[str]:
        """
        Return RBAC roles for the given Mattermost identity.

        Lookup order: username → user_id → default_role.
        If auth is disabled, always returns [default_role].
        """
        if not self.enabled:
            return [self.default_role]

        roles = self.user_roles.get(username) or self.user_roles.get(user_id)
        if roles:
            logger.debug(
                f"MattermostAuthManager: @{username!r} (id={user_id!r}) -> roles={roles}"
            )
            return roles

        logger.debug(
            f"MattermostAuthManager: unknown user @{username!r} (id={user_id!r}), "
            f"assigning default_role={self.default_role!r}"
        )
        return [self.default_role]

    def build_context(
        self, user_id: str, username: str = "", email: str = ""
    ) -> MattermostUserContext:
        """Build a MattermostUserContext for the given user."""
        roles = self.get_roles(username=username, user_id=user_id)
        return MattermostUserContext(
            user_id=user_id,
            username=username,
            roles=roles,
            email=email,
        )

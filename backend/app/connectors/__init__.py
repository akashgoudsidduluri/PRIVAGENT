from .gmail import GmailConnector
from .google_calendar import GoogleCalendarConnector
from .google_drive import GoogleDriveConnector
from .slack import SlackConnector
from .github import GitHubConnector
from .outlook import OutlookConnector
from .local_filesystem import LocalFileSystemAdapter

__all__ = ["GmailConnector", "GoogleCalendarConnector", "GoogleDriveConnector", "SlackConnector", "GitHubConnector", "OutlookConnector", "LocalFileSystemAdapter"]

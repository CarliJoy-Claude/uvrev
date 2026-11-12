"""Exceptions of uvrev that are not raised by click itself.

``ReportingCommand`` (``cli.py``) turns them into a message for the user.
"""


class RevisionNotFoundError(Exception): ...


class ToolError(Exception):
    """A tool can not be installed or run by ``uvrev tool run``"""

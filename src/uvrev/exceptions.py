class RevisionNotFoundError(Exception): ...


class ToolError(Exception):
    """A tool can not be installed or run by ``uvrev tool run``"""

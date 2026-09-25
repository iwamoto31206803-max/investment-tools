class R01Error(Exception):
    """Base fail-closed error."""


class PackageValidationError(R01Error):
    """The input is not a valid supported R01 package."""


class PlanValidationError(R01Error):
    """An acquisition request has an invalid state."""

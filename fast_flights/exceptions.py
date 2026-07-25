class FlightsResponseError(Exception):
    """Google Flights returned a response that could not be parsed."""


class FlightsNotFound(Exception):
    """No flights were found."""

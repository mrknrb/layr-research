"""The plugin's own error: a problem said so a person (or an AI) can fix it."""


class LabError(ValueError):
    """A mistake in what was asked (an unknown id, a missing file, a setting without a value), said plainly."""

"""Domain errors for immutable reconstruction artifacts."""


class FrozenArtifactError(FileExistsError):
    """Raised when a write would replace an already frozen artifact."""

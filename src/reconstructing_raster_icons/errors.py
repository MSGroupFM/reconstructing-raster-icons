"""Domain errors for immutable reconstruction artifacts."""


class InvalidInputError(ValueError):
    """Raised when a source cannot satisfy the raster intake contract."""


class FrozenArtifactError(FileExistsError):
    """Raised when a write would replace an already frozen artifact."""

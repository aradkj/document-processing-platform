"""Storage subsystem exception hierarchy."""


class StorageError(Exception):
    """Base exception for all storage-related errors."""

    pass


class StorageConfigurationError(StorageError):
    """Raised when storage backend configuration is invalid, incomplete, or unsupported."""

    pass


class StorageUnsupportedFilesystemError(StorageConfigurationError):
    """Raised when the filesystem lacks required atomic primitives (e.g. hard links)."""

    pass


class StorageInvalidKeyError(StorageError):
    """Raised when a storage key is malformed, invalid, or violates path safety constraints."""

    pass


class StorageObjectNotFoundError(StorageError):
    """Raised when the requested storage object does not exist."""

    pass


class StorageObjectAlreadyExistsError(StorageError):
    """Raised when attempting to store an object with a key that already exists."""

    pass


class StoragePermissionError(StorageError):
    """Raised when the storage backend encounters filesystem or remote permission errors."""

    pass


class StorageWriteError(StorageError):
    """Raised when writing or persisting an object stream fails."""

    pass


class StorageReadError(StorageError):
    """Raised when opening or streaming data from a stored object fails."""

    pass


class StorageDeleteError(StorageError):
    """Raised when deleting a stored object from the backend fails."""

    pass


class StorageSecurityError(StorageError):
    """Raised when an active security violation (e.g. symlink traversal) is detected."""

    pass

"""The errnos that mean a filesystem has no ``F_FULLFSYNC`` (SMB, NFS and some FUSE mounts answer one of them), so a
flush falls back to a plain ``fsync``. Any other error, EIO above all, is a failed flush and propagates. One set, read
by every flush that has the fallback. Stdlib only, 3.9-valid."""
import errno

FULLFSYNC_FALLBACK_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTTY, errno.ENOSYS,
                                       errno.ENODEV})

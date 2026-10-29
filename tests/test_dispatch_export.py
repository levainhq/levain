"""levain.dispatch.export: the reader of root's dispatch-artifact.json accepts only the whole, well-formed record."""
from __future__ import annotations

import json
import os

import pytest

from levain.dispatch import export as X

SESSION = "s-" + "0123456789abcdef"
DIGEST = "sha256:" + "a" * 64


def good(**over) -> dict:
    d = {"format": X.FORMAT, "session": SESSION, "ticket": "tkt-1", "manifest_digest": DIGEST, "bundle": "bundle",
         "bundle_digest": "sha256:" + "b" * 64, "files": 3, "bytes": 120}
    d.update(over)
    return d


def put(tmp_path, obj=None, *, raw: bytes | None = None, session=SESSION):
    d = tmp_path / session
    d.mkdir()
    (d / X.FILE).write_bytes(raw if raw is not None else json.dumps(obj).encode())
    return tmp_path


def refused(tmp_path, name):
    with pytest.raises(X.ArtifactUnreadable) as e:
        X.read_artifact(tmp_path, SESSION)
    assert e.value.name == name
    return e.value


def test_a_whole_artifact_reads(tmp_path):
    a = X.read_artifact(put(tmp_path, good()), SESSION)
    assert (a.session, a.ticket, a.bundle, a.files, a.bytes) == (SESSION, "tkt-1", "bundle", 3, 120)
    assert a.bundle_digest == "sha256:" + "b" * 64 and a.manifest_digest == DIGEST


def test_an_adopt_nothing_new_artifact_has_both_bundle_fields_null(tmp_path):
    a = X.read_artifact(put(tmp_path, good(bundle=None, bundle_digest=None)), SESSION)
    assert a.bundle is None and a.bundle_digest is None


def test_no_file_is_absent(tmp_path):
    (tmp_path / SESSION).mkdir()
    refused(tmp_path, "absent")


def test_no_session_directory_is_absent(tmp_path):
    refused(tmp_path, "absent")


@pytest.mark.parametrize("session", ["", "s-xyz", "../s-0123456789abcdef", "S-0123456789abcdef", None, 5])
def test_a_session_that_is_no_session_id_never_touches_the_disk(tmp_path, session):
    with pytest.raises(X.ArtifactUnreadable) as e:
        X.read_artifact(tmp_path, session)
    assert e.value.name == "bad-session"


@pytest.mark.parametrize("over", [
    {"bundle": None},                                    # a null bundle with a digest
    {"bundle_digest": None},                             # a bundle with no digest
    {"bundle": "other"},
    {"bundle_digest": "sha256:xyz"},
    {"manifest_digest": "md5:abc"},
    {"ticket": ""}, {"ticket": 5},
    {"files": -1}, {"bytes": -1}, {"files": True}, {"bytes": "9"}, {"files": 1.5},
    {"session": "s-" + "f" * 16},
    {"format": "levain-dispatch-artifact/2"},
])
def test_a_field_off_shape_is_never_an_artifact(tmp_path, over):
    put(tmp_path, good(**over))
    with pytest.raises(X.ArtifactUnreadable):
        X.read_artifact(tmp_path, SESSION)


@pytest.mark.parametrize("drop", sorted(X._KEYS))
def test_a_missing_key_is_never_an_artifact(tmp_path, drop):
    obj = good()
    del obj[drop]
    put(tmp_path, obj)
    refused(tmp_path, "shape")


def test_a_file_with_no_format_key_reads(tmp_path):
    """The contract does not define a `format` key in the file: a reader that refused every file without one would be
    a silent dead path against the real writer. A `format` that IS present must be the known value."""
    obj = good()
    del obj["format"]
    a = X.read_artifact(put(tmp_path, obj), SESSION)
    assert a.ticket == "tkt-1"


def test_an_extra_key_is_never_an_artifact(tmp_path):
    put(tmp_path, good(extra=1))
    refused(tmp_path, "shape")


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b'{"a":1,"a":2}', b"\xef\xbb\xbf{}", b"\xff",
                                 b'{"files":1.0}', b'{"files":NaN}'])
def test_bytes_that_do_not_parse_are_never_an_artifact(tmp_path, raw):
    put(tmp_path, raw=raw)
    with pytest.raises(X.ArtifactUnreadable):
        X.read_artifact(tmp_path, SESSION)


def test_a_duplicate_key_is_unparseable_not_last_wins(tmp_path):
    raw = json.dumps(good()).encode()[:-1] + b',"bytes":1}'
    put(tmp_path, raw=raw)
    refused(tmp_path, "unparseable")


def test_an_oversize_file_is_refused_unread(tmp_path):
    put(tmp_path, raw=b" " * (X.MAX_BYTES + 1))
    refused(tmp_path, "too-large")


def test_a_symlinked_file_is_refused(tmp_path):
    d = tmp_path / SESSION
    d.mkdir()
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps(good()))
    os.symlink(other, d / X.FILE)
    refused(tmp_path, "unopenable")


def test_a_symlinked_session_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / X.FILE).write_text(json.dumps(good()))
    os.symlink(real, tmp_path / SESSION)
    refused(tmp_path, "unopenable")


def test_a_fifo_is_refused_without_blocking(tmp_path):
    d = tmp_path / SESSION
    d.mkdir()
    os.mkfifo(d / X.FILE)
    refused(tmp_path, "not-regular")


def test_a_directory_in_place_of_the_file_is_refused(tmp_path):
    (tmp_path / SESSION / X.FILE).mkdir(parents=True)
    with pytest.raises(X.ArtifactUnreadable):
        X.read_artifact(tmp_path, SESSION)


# -- G1: an operational I/O failure is not an unreadable artifact ---------------------------------------------------

@pytest.mark.parametrize("err", [24, 23, 5, 13, 28])          # EMFILE, ENFILE, EIO, EACCES, ENOSPC
def test_an_operational_failure_opening_the_file_is_not_unreadable(tmp_path, monkeypatch, err):
    """The artifact is there and well-formed; the open fails for the machine's reasons. Passing it as `unreadable`
    would move the cursor past the only copy, so the failure is its own type and the caller retries."""
    put(tmp_path, good())
    real = os.open

    def flaky(path, flags, *a, **kw):
        if path == X.FILE:
            raise OSError(err, os.strerror(err))
        return real(path, flags, *a, **kw)
    monkeypatch.setattr(X.os, "open", flaky)
    with pytest.raises(X.ArtifactIOError) as e:
        X.read_artifact(tmp_path, SESSION)
    assert not isinstance(e.value, X.ArtifactUnreadable)


def test_an_operational_failure_reading_the_file_is_not_unreadable(tmp_path, monkeypatch):
    put(tmp_path, good())

    def eio(fd, n):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(X.os, "read", eio)
    with pytest.raises(X.ArtifactIOError):
        X.read_artifact(tmp_path, SESSION)


def test_an_operational_failure_opening_the_session_directory_is_not_unreadable(tmp_path, monkeypatch):
    put(tmp_path, good())
    real = os.open

    def flaky(path, flags, *a, **kw):
        if str(path).endswith(SESSION):
            raise OSError(24, "Too many open files")
        return real(path, flags, *a, **kw)
    monkeypatch.setattr(X.os, "open", flaky)
    with pytest.raises(X.ArtifactIOError):
        X.read_artifact(tmp_path, SESSION)


def test_a_short_read_of_a_stable_file_is_read_to_the_end(tmp_path, monkeypatch):
    """A read that returns fewer bytes than the file holds is not a changed file."""
    put(tmp_path, good())
    real = os.read
    monkeypatch.setattr(X.os, "read", lambda fd, n: real(fd, min(n, 7)))
    assert X.read_artifact(tmp_path, SESSION).ticket == "tkt-1"


def test_an_operational_failure_statting_the_file_is_not_unreadable(tmp_path, monkeypatch):
    """H1: the artifact opened; the fstat of it fails for the machine's reasons, which is ArtifactIOError by name."""
    put(tmp_path, good())

    def eio(fd):
        raise OSError(5, "Input/output error")
    # only the export module sees the failure: patching os.fstat itself reaches every other thread and library in the process
    import types
    monkeypatch.setattr(X, "os", types.SimpleNamespace(**{**vars(os), "fstat": eio}))
    with pytest.raises(X.ArtifactIOError) as e:
        X.read_artifact(tmp_path, SESSION)
    assert e.value.name == "stat"

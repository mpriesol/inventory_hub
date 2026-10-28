"""Generated delivery credentials on the existing private persistent mount."""
import hashlib
import os
import re
import secrets
import stat
import tempfile
from inventory_hub.settings import settings


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _root():
    root = settings.SUPPLIER_CREDENTIALS_ROOT
    public = settings.INVENTORY_DATA_ROOT.resolve()
    resolved = root.resolve()
    if (not root.is_absolute() or root.is_symlink() or resolved == public or public in resolved.parents
            or resolved in public.parents):
        raise ValueError('Private credential mount is required')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError('Private credential owner mismatch')
    root.chmod(0o700)
    return root


def _filename(shop_id, secret_hash):
    if type(shop_id) is not int or shop_id < 1 or not re.fullmatch(r'[0-9a-f]{64}', secret_hash):
        raise ValueError('Invalid credential identifier')
    return f'order-webhook-{shop_id}-{secret_hash}.secret'


def create(shop_id):
    # Versioned files preserve the old active credential if the DB transaction
    # rolls back. Raw secrets never enter SQL, responses, URLs in logs, or Git.
    token = secrets.token_urlsafe(48)
    secret_hash = digest(token)
    root = _root()
    fd, temporary = tempfile.mkstemp(prefix='.order-webhook-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / _filename(shop_id, secret_hash))
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return secret_hash


def load(shop_id, secret_hash):
    path = _root() / _filename(shop_id, secret_hash)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='ascii') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 200):
            raise ValueError('Invalid credential file')
        value = stream.read(201)
    if not re.fullmatch(r'[A-Za-z0-9_-]{64}', value) or not secrets.compare_digest(digest(value), secret_hash):
        raise ValueError('Credential mismatch')
    return value

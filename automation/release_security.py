"""Minimal signature verification shared with the standalone updater."""
import base64
import json
from packaging.version import Version
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def verify_manifest(raw, signature, public_key):
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(signature, raw)
    manifest = json.loads(raw)
    if manifest.get('app') != 'TayyaraMotors' or not isinstance(manifest.get('sha256'), str) or len(manifest['sha256']) != 64:
        raise ValueError('Invalid release manifest')
    if manifest.get('reset_databases', []) not in ([], ['listings']):
        raise ValueError('Unsupported database reset request')
    Version(manifest['version'])
    return manifest

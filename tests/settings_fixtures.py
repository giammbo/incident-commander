"""Complete, synthetic service-account material for local structural validation."""

import json
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat


@lru_cache
def service_account_json(client_email="bot@project.iam.gserviceaccount.com"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "synthetic-project",
            "private_key_id": "synthetic-key",
            "private_key": key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ).decode(),
            "client_email": client_email,
            "client_id": "123456789",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )

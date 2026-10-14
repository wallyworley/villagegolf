#!/usr/bin/env python3
"""Print a new VAPID key pair in .env form.

Usage: ``.venv/bin/python scripts/generate_vapid_keys.py``

Run it once, add the three lines to the server .env, and keep them. Rotating the
keys invalidates every device's notification subscription.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate():
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url(private), b64url(public)


def main():
    private, public = generate()
    print(f"VAPID_PRIVATE_KEY={private}")
    print(f"VAPID_PUBLIC_KEY={public}")
    print("VAPID_SUBJECT=https://villagefairways.com")


if __name__ == "__main__":
    main()

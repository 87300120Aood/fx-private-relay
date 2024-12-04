# Inspired by django-bouncy utils:
# https://github.com/organizerconnect/django-bouncy/blob/master/django_bouncy/utils.py

import base64
import logging
from typing import Any
from urllib.request import urlopen

from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import SuspiciousOperation

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger("events")

NOTIFICATION_HASH_FORMAT = """\
Message
{Message}
MessageId
{MessageId}
Subject
{Subject}
Timestamp
{Timestamp}
TopicArn
{TopicArn}
Type
{Type}
"""

NOTIFICATION_WITHOUT_SUBJECT_HASH_FORMAT = """\
Message
{Message}
MessageId
{MessageId}
Timestamp
{Timestamp}
TopicArn
{TopicArn}
Type
{Type}
"""

SUBSCRIPTION_HASH_FORMAT = """\
Message
{Message}
MessageId
{MessageId}
SubscribeURL
{SubscribeURL}
Timestamp
{Timestamp}
Token
{Token}
TopicArn
{TopicArn}
Type
{Type}
"""

SUPPORTED_SNS_TYPES = [
    "SubscriptionConfirmation",
    "Notification",
]


class VerificationFailed(ValueError):
    pass


def verify_from_sns(json_body: dict[str, Any]) -> dict[str, Any]:
    """
    Raise an exception if SNS signature verification fails.

    https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html

    Only supports SignatureVersion 1. SignatureVersion 2 (SHA256) was added in
    September 2022, and requires opt-in.
    """
    signing_cert_url = json_body["SigningCertURL"]
    cert_pubkey = _get_signing_public_key(signing_cert_url)
    signature = base64.decodebytes(json_body["Signature"].encode())
    hash_format = _get_hash_format(json_body)

    try:
        cert_pubkey.verify(
            signature,
            hash_format.format(**json_body).encode(),
            padding.PKCS1v15(),
            hashes.SHA1(),  # noqa: S303  # Use of insecure hash SHA1
        )
    except InvalidSignature as e:
        raise VerificationFailed(
            f"Invalid signature with SigningCertURL {signing_cert_url}"
        ) from e

    return json_body


def _get_hash_format(json_body: dict[str, Any]) -> str:
    message_type = json_body["Type"]
    if message_type == "Notification":
        if "Subject" in json_body.keys():
            return NOTIFICATION_HASH_FORMAT
        return NOTIFICATION_WITHOUT_SUBJECT_HASH_FORMAT

    return SUBSCRIPTION_HASH_FORMAT


def _get_signing_public_key(cert_url: str) -> rsa.RSAPublicKey:
    pemfile = _grab_keyfile(cert_url)

    # Extract the first certificate in the file and confirm it's a valid
    # PEM certificate
    certs = x509.load_pem_x509_certificates(pemfile)
    # A proper certificate file will contain 1 certificate
    if len(certs) != 1:
        raise VerificationFailed(
            f"SigningCertURL {cert_url} has {len(certs)} certificates."
        )
    cert_pubkey = certs[0].public_key()
    if not isinstance(cert_pubkey, rsa.RSAPublicKey):
        raise VerificationFailed(f"SigningCertURL {cert_url} is not an RSA key")
    return cert_pubkey


def _grab_keyfile(cert_url: str) -> bytes:
    cert_url_origin = f"https://sns.{settings.AWS_REGION}.amazonaws.com/"
    if not (cert_url.startswith(cert_url_origin)):
        raise SuspiciousOperation(
            f'SNS SigningCertURL "{cert_url}" did not start with "{cert_url_origin}"'
        )

    key_cache = caches[getattr(settings, "AWS_SNS_KEY_CACHE", "default")]

    pemfile = key_cache.get(cert_url)
    if not pemfile:
        response = urlopen(cert_url)  # noqa: S310 (check for custom scheme)
        pemfile = response.read()

        # Extract the first certificate in the file and confirm it's a valid
        # PEM certificate
        certs = x509.load_pem_x509_certificates(pemfile)
        # A proper certificate file will contain 1 certificate
        if len(certs) != 1:
            raise VerificationFailed(
                f"SigningCertURL {cert_url} has {len(certs)} certificates."
            )
        cert_pubkey = certs[0].public_key()
        if not isinstance(cert_pubkey, rsa.RSAPublicKey):
            raise VerificationFailed(f"SigningCertURL {cert_url} is not an RSA key")

        key_cache.set(cert_url, pemfile)
    if not isinstance(pemfile, bytes):  # pragma: no cover
        raise ValueError(f"pemfile is {type(pemfile)}, not bytes")
    return pemfile

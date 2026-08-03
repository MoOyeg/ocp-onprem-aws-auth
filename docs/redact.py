#!/usr/bin/env python3
"""Redact account-identifying and secret material from captured output.

Structure is preserved deliberately -- an ARN still looks like an ARN -- so the
capture stays explanatory while disclosing nothing.
"""
import os
import re
import subprocess
import sys

FAKE_ACCOUNT = "111122223333"          # AWS's documentation-reserved example id


def _account_id():
    """The account to scrub, discovered rather than hard-coded.

    Committing a real account id into the redaction script would defeat the
    point of the script. Takes AWS_ACCOUNT_ID if set, otherwise asks STS. Falls
    back to a generic 12-digit match so the pass still does something useful
    with no AWS access at all.
    """
    env = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    if env.isdigit():
        return env
    try:
        out = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout.strip()
        if out.isdigit():
            return out
    except Exception:  # noqa: BLE001 - no AWS access is a normal case here
        pass
    return None


ACCOUNT = _account_id()

SUBS = [
    # Account id, everywhere it appears (ARNs, bucket names, issuer URL).
    # With no account discovered, fall back to any 12-digit run that is not
    # already the placeholder -- blunter, but it fails safe.
    (re.compile(re.escape(ACCOUNT)) if ACCOUNT
     else re.compile(r'\b(?!' + FAKE_ACCOUNT + r'\b)\d{12}\b'), FAKE_ACCOUNT),
    # STS session / access key ids.
    (re.compile(r'\bASIA[A-Z0-9]{12,20}\b'), 'ASIAEXAMPLEEXAMPLE00'),
    (re.compile(r'\bAKIA[A-Z0-9]{12,20}\b'), 'AKIAEXAMPLEEXAMPLE00'),
    # IAM unique-id prefixes: role (AROA), user (AIDA).
    (re.compile(r'\bAROA[A-Z0-9]{12,24}\b'), 'AROAEXAMPLEEXAMPLE00'),
    (re.compile(r'\bAIDA[A-Z0-9]{12,24}\b'), 'AIDAEXAMPLEEXAMPLE00'),
    # Resource UUIDs (trust anchor, profile, CA, oidc provider).
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'),
     'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'),
    # Certificate serials and fingerprints (long hex runs).
    (re.compile(r'\b[0-9a-fA-F]{32,}\b'), '<hex-redacted>'),
    # Anything that looks like key material or a bearer token.
    (re.compile(r'\bIQoJ[A-Za-z0-9+/=]{8,}'), '<session-token-redacted>'),
    (re.compile(r'\beyJ[A-Za-z0-9_\-]{16,}'), '<jwt-redacted>'),
    (re.compile(r'(aws_secret_access_key\s*=\s*)\S+', re.I), r'\1<redacted>'),
    (re.compile(r'(aws_session_token\s*=\s*)\S+', re.I), r'\1<redacted>'),
    (re.compile(r'("SecretAccessKey"\s*:\s*")[^"]*'), r'\1<redacted>'),
    (re.compile(r'("Token"\s*:\s*")[^"]*'), r'\1<redacted>'),
    # token-info shows a short prefix before its own "<redacted>" marker so you
    # can eyeball which value you are looking at. Four characters of a 40-char
    # secret is not much, but a published capture should carry none of it.
    (re.compile(r'\S*…<redacted[^>]*>'), '<redacted>'),
    # Vault dev-mode root token, in case it is echoed anywhere.
    (re.compile(r'VAULT_TOKEN=\S+'), 'VAULT_TOKEN=<redacted>'),
]


def redact(text: str) -> str:
    for pattern, replacement in SUBS:
        text = pattern.sub(replacement, text)
    return text


if __name__ == "__main__":
    sys.stdout.write(redact(sys.stdin.read()))

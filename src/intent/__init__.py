"""Intent authority for the Unity generation pipeline.

Producers may append evidence to an :class:`IntentLedger`; only the resolver
may turn that evidence into canonical schema decisions.
"""

from .claims import IntentClaim, claims_from_classifier, claims_from_schema
from .ledger import IntentLedger
from .resolver import IntentResolution, resolve_intent

__all__ = [
    "IntentClaim",
    "IntentLedger",
    "IntentResolution",
    "claims_from_classifier",
    "claims_from_schema",
    "resolve_intent",
]

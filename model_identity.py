"""Credential-free backend identities for research cache and assessment isolation."""
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit


def backend_identity(config):
    """Do not conflate equal model names served by different backends.

    Persist only a digest: neither credentials nor the endpoint itself belongs
    in results. Price, display name and API-key rotation do not change identity.
    """
    parsed = urlsplit(config.base_url)
    endpoint = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                          parsed.path.rstrip('/'), '', ''))
    packet = [config.provider, endpoint, config.protocol, config.model]
    return hashlib.sha256(json.dumps(packet, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def research_model_identity(config):
    from model_policy import research_policy_identity
    identity=backend_identity(config)
    policy=research_policy_identity(config)
    return hashlib.sha256((identity+':'+policy).encode()).hexdigest() if policy else identity


def assessment_prompt_version(config):
    if not config.research_mode:
        return 'assess-v2:' + backend_identity(config)
    return 'assess-research-v2:' + research_model_identity(config)

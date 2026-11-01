"""Versioned, provider-specific inference capabilities; no guessed parameters.

MiniMax documents M3.1 Flash as always-thinking with configurable effort and a
Max default. Mechanical harness stages do not need that default. This policy is
limited to the exact documented family on official endpoints; unknown providers
retain their defaults. API acceptance and end-to-end outcomes are live-checked.
"""
from urllib.parse import urlsplit

VERSION='minimax-m31-stage-effort-v2'
REFERENCE='https://agent.minimax.io/tools/m3.1-flash'
STAGES={'plan','update','action','refine','assess','research_plan','research_coverage','probe','synthesis'}

def inference_policy(config,stage):
    host=(urlsplit(config.base_url).hostname or '').casefold()
    model=str(config.model).casefold()
    supported=(config.provider=='openai' and
        host in ('api.minimaxi.com','api.minimax.io','api.minimax.cn') and
        model in ('minimax-m3.1-flash','minimax-m3.1-flash-preview') and
        config.protocol in ('responses','chat_completions') and
        (config.research_mode or stage=='probe') and stage in STAGES)
    if not supported:return {'policy_version':'provider_default','reasoning_effort':None}
    return {'policy_version':VERSION,'reasoning_effort':'medium' if stage in ('assess','research_coverage','synthesis') else 'low',
            'reference':REFERENCE}

def research_policy_identity(config):
    policy=inference_policy(config,'assess')
    return policy['policy_version'] if policy['reasoning_effort'] else ''


def tolerates_partial_strict(config):
    """Keep the strict request only where it was live-verified to be accepted.

    M3.1 Flash answered HTTP 200 for our partial-`required` schema with
    `strict: true`; OpenAI-style validation rejects the same payload with HTTP
    400 (`'required' ... including every key in properties`), which is why
    strict mode is a per-vendor capability rather than a global default.
    Every other backend gets the non-strict `json_schema` dialect.
    """
    host=(urlsplit(getattr(config,'base_url','')).hostname or '').casefold()
    model=str(getattr(config,'model','')).casefold()
    return (getattr(config,'provider','')=='openai' and
            host in ('api.minimaxi.com','api.minimax.io','api.minimax.cn') and
            model in ('minimax-m3.1-flash','minimax-m3.1-flash-preview'))

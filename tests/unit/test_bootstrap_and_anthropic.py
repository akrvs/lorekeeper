"""Bootstrap sweep (missing-vocabulary proposals) + Anthropic provider mapping."""

from types import SimpleNamespace

import pytest

from app.bootstrap import BootstrapFindings, run_bootstrap
from app.config import settings
from app.db.models import RawDocument
from app.db.models.proposal import Proposal
from app.llm.anthropic import AnthropicProvider
from app.llm.base import LLMError, LLMRefusal
from app.llm.stub import StubProvider
from app.ontology.schema import UnmappedType


class MissingTypeProvider(StubProvider):
    """Deterministic bootstrap reviewer: every sampled doc is missing 'okr'."""

    def extract(self, system_prompt, user_content, schema):
        assert "BOOTSTRAP MODE" in system_prompt
        return BootstrapFindings(
            missing_types=[
                UnmappedType(
                    kind="node", name="okr", description="A quarterly objective.", example="Q3 OKR"
                )
            ]
        )


def test_bootstrap_samples_docs_and_files_deduplicated_proposals(db):
    for i in range(3):
        db.add(
            RawDocument(
                source_system="notion", source_type="page", external_id=f"p{i}", content="Q3 OKR…"
            )
        )
    db.flush()

    report = run_bootstrap(db, provider=MissingTypeProvider(), sample=10)
    assert report["documents_sampled"] == 3
    assert report["proposals_filed"] == 1  # same missing type across docs -> one proposal
    assert report["proposed"] == ["schema_node_type:okr"]
    p = db.query(Proposal).filter(Proposal.dedup_key == "okr").one()
    assert p.agent == "bootstrap" and p.status == "pending"


def test_stub_provider_returns_empty_findings_for_bootstrap_schema():
    findings = StubProvider(8).extract("prompt", "content", BootstrapFindings)
    assert findings.missing_types == []


# --- Anthropic provider (fake client — no network) ---------------------------
def _provider(parse_result=None, parse_exc=None) -> AnthropicProvider:
    def parse(**kwargs):
        if parse_exc is not None:
            raise parse_exc
        return parse_result

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    return AnthropicProvider(settings, client=client)


def test_anthropic_extract_returns_parsed_output():
    findings = BootstrapFindings()
    provider = _provider(SimpleNamespace(stop_reason="end_turn", parsed_output=findings))
    assert provider.extract("sys", "user", BootstrapFindings) is findings


def test_anthropic_refusal_and_truncation_map_to_llm_errors():
    refusing = _provider(SimpleNamespace(stop_reason="refusal", parsed_output=None))
    with pytest.raises(LLMRefusal):
        refusing.extract("sys", "user", BootstrapFindings)

    truncated = _provider(SimpleNamespace(stop_reason="max_tokens", parsed_output=None))
    with pytest.raises(LLMError, match="max_tokens"):
        truncated.extract("sys", "user", BootstrapFindings)


def test_anthropic_embeddings_delegate_to_deterministic_stub():
    provider = _provider()
    vectors = provider.embed(["checkout-v2"])
    assert vectors == StubProvider(settings.embedding_dim).embed(["checkout-v2"])

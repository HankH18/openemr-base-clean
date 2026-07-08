"""Eval suite — full-pipeline tests against seeded-style FHIR fixtures.

Runs the synthesizer + verification stack end-to-end. Deterministic
cases require no API key. LLM-judge cases are ``@pytest.mark.llm`` and
skip when ``ANTHROPIC_API_KEY`` is not set.
"""

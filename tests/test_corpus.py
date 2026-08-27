import pytest

from kneepoint.generator.corpus import DEFAULT_PROMPTS, CorpusSampler


def test_default_prompts_are_varied():
    assert len(DEFAULT_PROMPTS) >= 5
    lengths = {len(p.split()) for p in DEFAULT_PROMPTS}
    assert len(lengths) >= 3  # short/medium/long mix, not clones


def test_sampler_is_seeded_and_deterministic():
    s1 = CorpusSampler(DEFAULT_PROMPTS, seed=42)
    s2 = CorpusSampler(DEFAULT_PROMPTS, seed=42)
    s3 = CorpusSampler(DEFAULT_PROMPTS, seed=43)
    seq1 = [s1.sample() for _ in range(10)]
    assert seq1 == [s2.sample() for _ in range(10)]      # same seed, same sequence
    assert seq1 != [s3.sample() for _ in range(10)]      # different seed diverges


def test_sampler_covers_corpus():
    sampler = CorpusSampler(DEFAULT_PROMPTS, seed=1)
    seen = {sampler.sample() for _ in range(200)}
    assert seen == set(DEFAULT_PROMPTS)


def test_from_glob_reads_example_corpus():
    sampler = CorpusSampler.from_glob("examples/prompts/support/*.txt", seed=0)
    prompt = sampler.sample()
    assert prompt and "\n\n" not in prompt


def test_empty_inputs_raise():
    with pytest.raises(ValueError):
        CorpusSampler([], seed=0)
    with pytest.raises(ValueError):
        CorpusSampler.from_glob("examples/prompts/does-not-exist/*.txt")

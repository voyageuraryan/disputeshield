from app.scripts.check_llm_keys import env_problems, fingerprint


def test_env_problems_spots_duplicates_quotes_and_lists(tmp_path):
    env = tmp_path / ".env"
    env.write_text('GROQ_API_KEY=gsk_one\nGEMINI_API_KEY=AIzaX\nGROQ_API_KEY="gsk_a, gsk_b"\n')
    out = " | ".join(env_problems(env))
    assert "GROQ_API_KEY is set 2 times" in out and "in quotes" in out and "spaces or commas" in out
    assert "gsk_a" not in out  # never prints a key


def test_fingerprint_hides_the_key():
    fp = fingerprint("gsk_secretsecretsecret")
    assert "secretsecret" not in fp and fp.startswith("gsk_")

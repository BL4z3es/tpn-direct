from tpn.proc import build_env, wait_for


def test_build_env_strips_inherited_proxies_when_going_direct():
    base = {"PATH": "/bin", "HTTPS_PROXY": "http://x:1", "all_proxy": "socks5://y:2"}
    assert build_env(None, base) == {"PATH": "/bin"}


def test_build_env_sets_both_spellings_of_the_proxy():
    env = build_env("http://127.0.0.1:9080", {"PATH": "/bin", "NO_PROXY": "*"})
    assert env["HTTPS_PROXY"] == env["http_proxy"] == "http://127.0.0.1:9080"
    assert "NO_PROXY" not in env


def test_wait_for_times_out(clock):
    assert not wait_for(lambda: False, 5, interval=1, clock=clock, sleep=clock.sleep)
    assert clock.now == 5


def test_wait_for_returns_once_predicate_holds(clock):
    answers = iter([False, False, True])
    assert wait_for(lambda: next(answers), 60, interval=1, clock=clock, sleep=clock.sleep)
    assert clock.now == 2

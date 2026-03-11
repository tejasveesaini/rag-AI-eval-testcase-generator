collect_ignore_glob = []


def pytest_configure(config):
    # Disable deepeval's pytest plugin — it intercepts the session and
    # requires a logged-in deepeval account. Our eval tests use the
    # evaluation module directly, not deepeval's pytest integration.
    config.pluginmanager.set_blocked("deepeval")

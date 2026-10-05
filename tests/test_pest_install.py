"""
Pins the command sequence of ``_build_inertia_project_zip_sync`` for
``testing_framework=pest``. ``_run`` is replaced by a recorder, so no Composer,
PHP or npm is needed.

Why the order matters: the starter kits ship a ``post-update-cmd`` hook that runs
``php artisan install:features`` until chisel has run. Any ``composer remove`` /
``composer require`` before the installer's own ``install:features`` step must
therefore pass ``--no-scripts``, otherwise chisel deletes itself early and the
real step fails.
"""

import pytest

from services import installer_service

CONTEXT = {
    "php_version": "8.4",
    "app_port": "8000",
    "max_upload_mb": 10,
    "post_max_mb": 12,
    "db": "sqlite",
    "queue": "database",
    "app_name": "demo",
    "starter_kit": "react",
    "auth_provider": "laravel",
    "teams": False,
    "testing_framework": "pest",
    "install_boost": False,
    "auth_features": ["registration"],
}


class Call:
    def __init__(self, cmd, env, check):
        self.cmd = cmd
        self.env = env
        self.check = check

    def __repr__(self):
        return " ".join(self.cmd)


@pytest.fixture
def calls(monkeypatch):
    recorded: list[Call] = []

    def fake_run(cmd, *, cwd, env, timeout, check=True):
        recorded.append(Call(cmd, env, check))
        if cmd[:2] == ["composer", "create-project"]:
            project = installer_service.Path(cwd) / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text("APP_KEY=\n")
        elif cmd[:3] == ["php", "artisan", "key:generate"]:
            (installer_service.Path(cwd) / ".env").write_text("APP_KEY=base64:test\n")

    monkeypatch.setattr(installer_service, "_run", fake_run)
    monkeypatch.setattr(installer_service, "_build_env", lambda: {})
    return recorded


def _generate(context=CONTEXT):
    installer_service._build_inertia_project_zip_sync(
        context, installer_service.INERTIA_SERVER_TEMPLATES_V2
    )


def _is_composer_mutation(call: Call) -> bool:
    return call.cmd[0] == "composer" and call.cmd[1] in {"remove", "require"}


def _index(calls, *prefix):
    return next(i for i, c in enumerate(calls) if c.cmd[: len(prefix)] == list(prefix))


def test_composer_calls_before_install_features_skip_scripts(calls):
    _generate()

    install_features = _index(calls, "php", "artisan", "install:features")
    mutations = [c for c in calls[:install_features] if _is_composer_mutation(c)]

    assert mutations, "the Pest swap should run composer remove/require"
    for call in mutations:
        assert "--no-scripts" in call.cmd, call


def test_install_features_runs_once_with_requested_answers(calls):
    _generate()

    runs = [c for c in calls if c.cmd[:3] == ["php", "artisan", "install:features"]]
    assert len(runs) == 1
    assert '--answers={"auth_features": ["registration"]}' in runs[0].cmd


def test_pest_is_initialised_with_the_binary_not_artisan(calls):
    _generate()

    assert not any("pest:install" in c.cmd for c in calls)

    require = _index(calls, "composer", "require", "pestphp/pest")
    init = _index(calls, "php", "./vendor/bin/pest", "--init")
    assert require < init
    assert calls[init].env["PEST_NO_SUPPORT"] == "true"


def test_pest_requires_the_laravel_plugin(calls):
    _generate()

    require = calls[_index(calls, "composer", "require", "pestphp/pest")]
    assert "pestphp/pest-plugin-laravel" in require.cmd
    assert "--dev" in require.cmd


def test_drift_converts_tests_after_chisel_and_is_removed_again(calls):
    # chisel prunes unselected features' tests by their PHPUnit method form, so
    # the conversion to Pest closures has to come after install:features.
    _generate()

    install_features = _index(calls, "php", "artisan", "install:features")
    install = _index(calls, "composer", "require", "pestphp/pest-plugin-drift")
    drift = _index(calls, "php", "./vendor/bin/pest", "--drift")
    remove = _index(calls, "composer", "remove", "pestphp/pest-plugin-drift")
    assert install_features < install < drift < remove
    assert calls[drift].env["PEST_NO_SUPPORT"] == "true"


def test_every_composer_mutation_skips_scripts(calls):
    _generate()

    mutations = [c for c in calls if _is_composer_mutation(c)]
    assert mutations
    for call in mutations:
        assert "--no-scripts" in call.cmd, call


def test_packages_are_discovered_after_the_pest_swap_and_before_chisel(calls):
    _generate()

    install_features = _index(calls, "php", "artisan", "install:features")
    last_composer = max(
        i for i, c in enumerate(calls[:install_features]) if _is_composer_mutation(c)
    )
    discovers = [
        i for i, c in enumerate(calls) if c.cmd[:3] == ["php", "artisan", "package:discover"]
    ]

    assert any(last_composer < i < install_features for i in discovers)


def test_phpunit_removal_failures_are_not_swallowed(calls):
    _generate()

    remove = calls[_index(calls, "composer", "remove", "phpunit/phpunit")]
    assert remove.check is True


def test_phpunit_projects_skip_the_pest_swap(calls):
    _generate({**CONTEXT, "testing_framework": "phpunit"})

    assert not any(_is_composer_mutation(c) for c in calls)
    assert not any("pest" in " ".join(c.cmd) for c in calls)

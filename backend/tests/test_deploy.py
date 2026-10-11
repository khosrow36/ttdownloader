import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'deploy.sh'
IMAGE = 'ghcr.io/example/ttdownloader@sha256:' + 'a' * 64


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    (root / '.secrets').mkdir()
    (root / '.secrets/app_password').write_text('test-password-only')
    for name in ('old', 'new'):
        release = root / 'releases' / name
        release.mkdir(parents=True)
        (release / 'compose.yaml').write_text('services: {}')
        (release / 'image.env').write_text(f'TTDOWNLOADER_IMAGE={IMAGE}\n')
    (root / 'current').symlink_to(root / 'releases/old')

    binaries = tmp_path / 'bin'
    binaries.mkdir()
    docker = binaries / 'docker'
    docker.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DEPLOY_TEST_LOG"
if [[ ${DEPLOY_TEST_FAIL:-} == pull && " $* " == *" pull "* ]]; then exit 1; fi
if [[ ${DEPLOY_TEST_FAIL:-} == start && "$*" == *releases/new/* && " $* " == *" up "* ]]; then exit 1; fi
''')
    docker.chmod(0o755)
    # macOS has no flock; these tests exercise transitions, not OS locking.
    flock = binaries / 'flock'
    flock.write_text('#!/bin/sh\nexit 0\n')
    flock.chmod(0o755)
    env = {
        'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
        'DEPLOY_TEST_LOG': str(tmp_path / 'docker.log'),
    }
    return root, env


def run_deploy(root, env, image=IMAGE):
    return subprocess.run(
        ['bash', str(SCRIPT), str(root), str(root / 'releases/new'), image],
        env=env, capture_output=True, text=True,
    )


def test_healthy_release_becomes_current(deployment):
    root, env = deployment
    assert run_deploy(root, env).returncode == 0
    assert (root / 'current').resolve() == root / 'releases/new'
    assert (root / 'previous').resolve() == root / 'releases/old'


def test_failed_start_restores_previous_release_and_reports_failure(deployment):
    root, env = deployment
    env['DEPLOY_TEST_FAIL'] = 'start'
    assert run_deploy(root, env).returncode != 0
    assert (root / 'current').resolve() == root / 'releases/old'
    commands = Path(env['DEPLOY_TEST_LOG']).read_text().splitlines()
    assert 'releases/old/compose.yaml up -d --wait' in commands[-1]


def test_failed_pull_does_not_restart_running_app(deployment):
    root, env = deployment
    env['DEPLOY_TEST_FAIL'] = 'pull'
    assert run_deploy(root, env).returncode != 0
    assert (root / 'current').resolve() == root / 'releases/old'
    assert ' up ' not in Path(env['DEPLOY_TEST_LOG']).read_text()


def test_failed_first_deployment_removes_unhealthy_container(deployment):
    root, env = deployment
    (root / 'current').unlink()
    env['DEPLOY_TEST_FAIL'] = 'start'
    assert run_deploy(root, env).returncode != 0
    assert not (root / 'current').exists()
    assert Path(env['DEPLOY_TEST_LOG']).read_text().splitlines()[-1].endswith(' down')


def test_rejects_mutable_image_tag(deployment):
    root, env = deployment
    assert run_deploy(root, env, 'ghcr.io/example/ttdownloader:latest').returncode != 0
    assert not Path(env['DEPLOY_TEST_LOG']).exists()

# Copyright 2026 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functest for MicroCeph storage role gating and reconciliation."""

import json
import logging
import os
import shutil
import tempfile

import jubilant
import pytest

from tests import helpers
from tests.functests.conftest import APP_NAME

logger = logging.getLogger(__name__)

METADATA_CONTENTS = """
name: dummy-provider
summary: Dummy role-assignment Provider charm for functional tests.
description: Dummy role-assignment Provider charm.
provides:
  role-assignment:
    interface: role-assignment
"""

LIFECYCLE_HOOK = "#!/bin/sh\nexit 0\n"


def create_dummy_provider_charm(target_dir):
    """Create a minimal bash-hook role-assignment Provider charm on the fly."""
    os.makedirs(target_dir, exist_ok=True)
    os.makedirs(os.path.join(target_dir, "hooks"), exist_ok=True)

    with open(os.path.join(target_dir, "metadata.yaml"), "w") as f:
        f.write(METADATA_CONTENTS)

    # All lifecycle hooks can just exit 0
    for hook in ["install", "start", "config-changed"]:
        hook_path = os.path.join(target_dir, "hooks", hook)
        with open(hook_path, "w") as f:
            f.write(LIFECYCLE_HOOK)
        os.chmod(hook_path, 0o755)

    # Relation hooks publish assignments giving microceph/0 the storage role
    assignments_json = json.dumps(
        {
            "microceph/0": {
                "status": "assigned",
                "roles": ["storage"],
                "workload-params": {},
            }
        }
    )

    relation_hook = f"""#!/bin/sh
if is-leader; then
  relation-set -r "$JUJU_RELATION_ID" --app assignments='{assignments_json}'
fi
exit 0
"""
    for hook in [
        "role-assignment-relation-joined",
        "role-assignment-relation-changed",
        "leader-settings-changed",
    ]:
        hook_path = os.path.join(target_dir, "hooks", hook)
        with open(hook_path, "w") as f:
            f.write(relation_hook)
        os.chmod(hook_path, 0o755)


@pytest.mark.abort_on_fail
def test_storage_role_gating_and_reconciliation(
    juju: jubilant.Juju, deployed_microceph: str, attached_lxd_volume: str
):
    """Verify that OSD enrollment is blocked when ineligible, and allowed once role-assigned."""
    unit_name = deployed_microceph
    disk_path = attached_lxd_volume

    # Enable role-managed mode (blocks any OSD enrollment since no relation exists yet)
    logger.info("Enabling role-managed placement mode")
    juju.config(APP_NAME, {"role-managed": True})

    # Wait for the change to settle
    with helpers.fast_forward(juju):
        helpers.wait_for_apps(juju, APP_NAME, timeout=300)

    # Verify that add-osd action fails gracefully when ineligible
    logger.info("Verifying add-osd fails while unit is not assigned the storage role")
    with pytest.raises(jubilant.TaskError) as exc_info:
        juju.run(unit_name, "add-osd", {"device-id": disk_path}, wait=120)
    assert "storage" in exc_info.value.task.message or "failed" in exc_info.value.task.status

    # Create and deploy our dummy role-assignment provider charm on the fly
    tmp_dir = tempfile.mkdtemp(prefix="dummy-provider-")
    try:
        logger.info(f"Creating dummy provider charm in temporary directory: {tmp_dir}")
        create_dummy_provider_charm(tmp_dir)

        logger.info("Deploying dummy role-assignment provider charm")
        juju.deploy(tmp_dir, "dummy-provider")

        # Relate microceph with dummy-provider
        logger.info("Relating microceph with dummy-provider")
        juju.integrate(f"{APP_NAME}:role-assignment", "dummy-provider:role-assignment")

        # Wait for both dummy-provider and microceph to stabilize
        with helpers.fast_forward(juju):
            helpers.wait_for_apps(juju, APP_NAME, timeout=600)

        # Run add-osd action which should now succeed since microceph/0 has the storage role
        logger.info("Verifying add-osd action succeeds now that unit is assigned the storage role")
        action = juju.run(unit_name, "add-osd", {"device-id": disk_path}, wait=1200)
        action.raise_on_failure()

        # Verify OSD was successfully enrolled in the cluster
        logger.info("Verifying OSD count")
        helpers.assert_osd_count(juju, APP_NAME, expected_osds=1)

    finally:
        # Cleanup temporary directory
        shutil.rmtree(tmp_dir, ignore_errors=True)

from astra.install import (
    create_default_policy,
    create_default_settings,
    create_roles,
    create_workspace,
    seed_workflow_packs,
)


def execute():
    create_roles()
    create_default_settings()
    create_workspace()
    create_default_policy()
    seed_workflow_packs()

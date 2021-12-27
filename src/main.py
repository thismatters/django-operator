import kopf

from django_operator.kinds import DjangoKind
from django_operator.utils import merge, superget


@kopf.on.update("thismatters.github", "v1alpha", "djangos")
@kopf.on.create("thismatters.github", "v1alpha", "djangos")
def begin_migration(patch, body, labels, diff, **kwargs):
    """Trigger the migration pipeline and update object to reflect migrating status"""
    for action, field, vals in diff:
        if action == "change" and field == ("metadata", "labels", "migration-step"):
            logger.debug("This change seems to be a migration step change. skipping")
            return
    if labels.get("migration-step", "ready") != "ready":
        raise kopf.TemporaryError("Cannot start a new migration right now", delay=30)
    kopf.info(body, reason="Migrating", message="Enacting new config")
    patch.status["condition"] = "migrating"
    patch.metadata.labels["migration-step"] = "starting"


@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "starting"}
)
def start_management_commands(logger, patch, body, spec, status, namespace, **kwargs):
    """Start the redis cache and kick off management commands"""
    django = DjangoKind(
        logger=logger,
        patch=patch,
        body=body,
        spec=spec,
        status=status,
        namespace=namespace,
    )
    logger.info("Setting up redis deployment")
    patch.status["created"] = django.ensure_redis()
    force_migrations = spec.get("alwaysRunMigrations")
    mgmt_pod = None
    migration_version = status.get("migrationVersion", "zero")
    if not force_migrations and migration_version == django.version:
        logger.info(
            f"Already migrated to version {django.version}, skipping "
            "management commands"
        )
    else:
        logger.info("Beginning management commands")
        mgmt_pod = django.start_manage_commands()
    patch.metadata.labels["migration-step"] = "mgmt-cmd"
    return {"pod_name": mgmt_pod}


@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "mgmt-cmd"}
)
def complete_management_commands(
    retries, logger, patch, body, spec, status, namespace, **kwargs
):
    """Ensure the management commands have completed, clean up their pod"""
    max_retries = superget(spec, "initManageTimeouts.iterations")
    if retries > max_retries:
        patch.status["condition"] = "degraded"
        raise kopf.PermanentError(
            "Migrations took too long. Manual intervention required!"
        )
    django = DjangoKind(
        logger=logger,
        patch=patch,
        body=body,
        spec=spec,
        status=status,
        namespace=namespace,
    )
    pod_name = superget(status, "start_management_commands.pod_name")
    if pod_name:
        pod_phase = django.pod_phase(pod_name)
        if pod_phase in ("failed", "unknown"):
            patch.status["condition"] = "degraded"
            raise kopf.PermanentError(
                "Migrations have failed. Manual intervention required!"
            )
        if pod_phase != "succeeded":
            period = superget(spec, "initManageTimeouts.period")
            raise kopf.TemporaryError(
                "The management commands have not completed. Waiting.", delay=period
            )
        django.clean_manage_commands(pod_name=pod_name)

    patch.status["migrationVersion"] = django.version
    blue_app = superget(status, "created.deployment.app")
    logger.info("Setting up green app deployment")
    patch.status["created"] = django.start_green_app()
    return {"blue_app": blue_app}


@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "green-app"}
)
def green_app_ready(retries, logger, patch, body, spec, status, namespace, **kwargs):
    """Ensure the green app has come up, complete process"""
    max_retries = 20
    if retries > max_retries:
        patch.status["condition"] = "degraded"
        raise kopf.PermanentError(
            "App not started in time. Manual intervention required!"
        )
    django = DjangoKind(
        logger=logger,
        patch=patch,
        body=body,
        spec=spec,
        status=status,
        namespace=namespace,
    )
    green = superget(status, "created.deployment.app")
    if not django.deployment_reached_condition(condition="Available", name=green):
        period = 6
        raise kopf.TemporaryError("Green app not available yet. Waiting.", delay=period)

    logger.info("Setting up green worker deployment")
    created = django.migrate_worker()
    logger.info("Setting up green beat deployment")
    merge(created, django.migrate_beat())
    logger.info("Migrating service to green app deployment")
    merge(created, django.migrate_service())
    blue_app = superget(status, "complete_management_commands.blue_app")
    logger.info("Removing blue app deployment")
    django.clean_blue_app(blue_app=blue_app)
    patch.metadata.labels["migration-step"] = "ready"
    patch.status["condition"] = "running"
    patch.status["version"] = django.version
    patch.status["replicas"] = {
        "app": django.base_kwargs["app_replicas"],
        "worker": django.base_kwargs["worker_replicas"],
    }
    patch.status["created"] = created
    kopf.info(body, reason="Ready", message="New config running")
    logger.info("Migration complete. All that was green is now blue")
    return {"ready": True}


# @kopf.on.timer("thismatters.net", "v1alpha", "djangos", interval=30)
# def scale_deployment(**kwargs):
#     DjangoKind().scale_deployment(**kwargs)
#     DjangoKind().scale_deployment(deployment="worker", **kwargs)

import kopf
from kubernetes.client.exceptions import ApiException

from django_operator.kinds import DjangoKind
from django_operator.utils import merge, superget


@kopf.on.update("thismatters.github", "v1alpha", "djangos")
@kopf.on.create("thismatters.github", "v1alpha", "djangos")
def begin_migration(logger, patch, body, labels, diff, spec, **kwargs):
    """Trigger the migration pipeline and update object to reflect migrating status"""

    # profile the incoming diff and squat on any changes apart from the
    # `migration-step` change
    migration_step_field = ("metadata", "labels", "migration-step")
    real_changes = False

    for action, field, old, new in diff:
        if field[0] != "metadata":
            logger.debug(
                f"Non metadata field {action} :: {field} := {old} -> {new}"
            )
            if labels.get("migration-step", "ready") == "ready":
                real_changes = True
            else:
                # my assumption here is that a relevant diff doesn't "time out"
                #  of the queue when other handlers run
                raise kopf.TemporaryError(
                    "Cannot start a new migration right now", delay=30
                )
    if not real_changes:
        logger.debug("Changes appear to only touch migration-step labels; skipping")
        # this is here to test whether a `create` event comes with a diff
        logger.debug(f"{diff}")
        return

    kopf.info(body, reason="Migrating", message="Enacting new config")
    patch.status["condition"] = "migrating"
    # collect _all_ the data needed for DjangoKind to run, store it in .status
    patch.status["migrateToSpec"] = dict(spec)
    patch.metadata.labels["migration-step"] = "starting"


@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "starting"}
)
def start_management_commands(logger, patch, body, status, namespace, **kwargs):
    """Start the redis cache and kick off management commands"""
    spec = status.get("migrateToSpec")
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
    logger, patch, body, status, namespace, retry, **kwargs
):
    """Ensure the management commands have completed, clean up their pod"""
    spec = status.get("migrateToSpec")
    max_retries = superget(spec, "initManageTimeouts.iterations")
    logger.debug(f"Retry count {retry}")
    if retry > max_retries:
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
        try:
            pod_phase = django.pod_phase(pod_name)
        except ApiException:
            pod_phase = "unknown"
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
    created = django.start_green_app()
    patch.status["created"] = created
    if blue_app == superget(created, "deployment.app"):
        # don't bonk out the thing you just created! (just in case the
        #  version didn't change)
        blue_app = None
    patch.metadata.labels["migration-step"] = "green-app"
    return {"blue_app": blue_app}


@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "green-app"}
)
def green_app_ready(logger, patch, body, status, namespace, retry, **kwargs):
    """Ensure the green app has come up, complete process"""
    spec = status.get("migrateToSpec")
    max_retries = 20
    logger.debug(f"Retry count {retry}")
    if retry > max_retries:
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
    patch.status["version"] = django.version
    patch.status["replicas"] = {
        "app": django.base_kwargs["app_replicas"],
        "worker": django.base_kwargs["worker_replicas"],
    }
    patch.status["created"] = created
    kopf.info(body, reason="Ready", message="New config running")
    logger.info("Migration complete. All that was green is now blue")
    patch.metadata.labels["migration-step"] = "cleanup"
    return {"migrated": True}


@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "cleanup"}
)
def complete_migration(logger, patch, spec, status, **kwargs):
    """Verify that the deployed spec is still the desired spec. Restart the
    migration process if the spec has changed"""
    deployed_spec = status.get("migrateToSpec")
    _spec = dict(spec)

    if deployed_spec == _spec:
        patch.status["condition"] = "running"
        patch.metadata.labels["migration-step"] = "ready"
        patch.status["migrateToSpec"] = None
    else:
        logger.info("Object changed during migration. Starting new migration.")
        patch.metadata.labels["migration-step"] = "starting"
        patch.status["migrateToSpec"] = _spec
    return {"complete": True}


# @kopf.on.timer("thismatters.net", "v1alpha", "djangos", interval=30)
# def scale_deployment(**kwargs):
#     DjangoKind().scale_deployment(**kwargs)
#     DjangoKind().scale_deployment(deployment="worker", **kwargs)

import kopf
from kubernetes.client.exceptions import ApiException

from django_operator.kinds import DjangoKind
from django_operator.utils import merge, superget


@kopf.on.create("thismatters.github", "v1alpha", "djangos")
def initial_migration(logger, patch, body, labels, spec, **kwargs):
    kopf.info(body, reason="Migrating", message="Enacting brand new config")
    patch.status["condition"] = "migrating"
    # collect _all_ the data needed for DjangoKind to run, store it in .status
    patch.status["migrateToSpec"] = dict(spec)
    patch.metadata.labels["migration-step"] = "starting"


@kopf.on.update("thismatters.github", "v1alpha", "djangos", labels={"migration-step": "ready"})
def begin_migration(logger, patch, body, labels, diff, spec, **kwargs):
    """Trigger the migration pipeline and update object to reflect migrating status"""

    # profile the incoming diff and squat on any changes apart from the
    # `migration-step` change
    real_changes = False

    for action, field, old, new in diff:
        if field[0] != "metadata":
            logger.debug(f"Non metadata field {action} :: {field} := {old} -> {new}")
            real_changes = True

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
    created = django.ensure_redis()
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
    return {"management_pod_name": mgmt_pod, "created": created}


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
    management_pod_name = superget(status, "start_management_commands.management_pod_name")

    if management_pod_name:
        try:
            pod_phase = django.pod_phase(management_pod_name)
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
        django.clean_manage_commands(pod_name=management_pod_name)

    blue_app = superget(status, "created.deployment.app")
    logger.info("Setting up green app deployment")
    created = django.start_green(purpose="app")

    patch.status["migrationVersion"] = django.version
    patch.metadata.labels["migration-step"] = "green-app"
    if blue_app == superget(created, "deployment.app"):
        # don't bonk out the thing you just created! (just in case the
        #  version didn't change)
        blue_app = None
    return {"blue_app": blue_app, "created": created}


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
    green = superget(status, "complete_management_commands.created.deployment.app")
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
    django.clean_blue(purpose="app", blue=blue_app)

    kopf.info(body, reason="Ready", message="New config running")
    logger.info("All that was green is now blue")
    patch.status["version"] = django.version
    patch.metadata.labels["migration-step"] = "cleanup"
    return {"created": created}

@kopf.on.update(
    "thismatters.github", "v1alpha", "djangos", labels={"migration-step": "cleanup"}
)
def complete_migration(logger, patch, spec, status, **kwargs):
    """Verify that the deployed spec is still the desired spec. Restart the
    migration process if the spec has changed"""
    deployed_spec = status.get("migrateToSpec")

    # clean up deployment
    # accumulate created resources
    created = {}
    creating_methods = (
        "green_app_ready", "start_management_commands", "complete_management_commands"
    )
    for method_name in creating_methods:
        merge(created, superget(status, f"{method_name}.created", default={}))
        patch.status[method_name] = None
    patch.status["created"] = created

    # is deployment complete?
    create_targets = [
        "deployment.app",
        "deployment.beat",
        "deployment.redis",
        "deployment.worker",
        # "horizontalpodautoscaler.app",
        # "horizontalpodautoscaler.worker",
        "ingress.app",
        "service.app",
        "service.redis",
    ]
    for purpose in ("app", "worker"):
        if superget(deployed_spec, f"autoscalers.{purpose}.enabled", default=False):
            create_targets.append(f"horizontalpodautoscaler.{purpose}")
    complete = all([superget(created, t) is not None for t in create_targets])

    # ensure latest spec is deployed
    _spec = dict(spec)
    if deployed_spec == _spec:
        if complete:
            patch.status["condition"] = "running"
            logger.info("Migration complete.")
        else:
            patch.status["condition"] = "degraded"
            logger.info("Something went wrong; manual intervention required")
        patch.metadata.labels["migration-step"] = "ready"
        patch.status["migrateToSpec"] = None
    else:
        logger.info("Object changed during migration. Starting new migration.")
        patch.metadata.labels["migration-step"] = "starting"
        patch.status["migrateToSpec"] = _spec
